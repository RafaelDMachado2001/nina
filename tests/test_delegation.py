import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fake_openai import FakeOpenAI, reply

from nina import orchestration
from nina.agents import managed
from nina.agents import roles as roles_mod

ROLES = roles_mod.load_roles()


class DelegationBase(unittest.TestCase):
    def setUp(self):
        self.api = FakeOpenAI()
        self.addCleanup(self.api.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)
        (Path(self.cwd) / "post.md").write_text("# Post\nUm texto com erro.\n", encoding="utf-8")
        p = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.cwd) / "state")})
        p.start()
        self.addCleanup(p.stop)
        self.events, self.lock = [], threading.Lock()
        self.runner = managed.Runner(self.emit)
        self.addCleanup(self.runner.shutdown)
        self.conn = {"id": "fake", "kind": "openai", "name": "FakeAI", "base_url": self.api.base_url, "model": "m",
                     "key": {"source": "none"}, "timeout_s": 10, "headers": {}, "context_window": 0,
                     "price_in": 0.0, "price_out": 0.0}
        self.other = {**self.conn, "id": "other", "name": "OtherAI", "model": "m2"}
        self.resolved = []

    def orchestrate(self, mode="coordinator", **kw):
        st = orchestration.settings({"orchestration": {"mode": mode, **kw}})

        def resolve(role, parent_conn):
            self.resolved.append((role["id"], parent_conn["id"]))
            conn = self.other if role["id"] == "writing-reviewer" else parent_conn
            return conn, conn["model"], "", None

        self.runner.orchestration = {"settings": st, "roles": ROLES, "resolve": resolve,
                                     "conn_name": lambda role_id: "OtherAI" if role_id == "writing-reviewer" else "FakeAI"}

    def emit(self, aid, kind, payload):
        with self.lock:
            self.events.append((aid, kind, payload))

    def kinds(self, aid=None):
        with self.lock:
            return [(k, p) for a, k, p in self.events if aid is None or a == aid]

    def wait_for(self, kind, pred=lambda p: True, timeout=20.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for k, p in self.kinds():
                if k == kind and pred(p):
                    return p
            time.sleep(0.01)
        self.fail(f"event {kind} never arrived; received: {self.kinds()}")

    def wait_idle(self, aid, n=1):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            if sum(1 for k, p in self.kinds(aid) if k == "status" and p == "idle") >= n:
                return
            time.sleep(0.01)
        self.fail(f"agent never got idle: {self.kinds()}")

    def start(self, prompt, role_id="coordinator"):
        return self.runner.start(ROLES[role_id], self.cwd, prompt, connection=self.conn, model="m")

    def delegate_call(self, cid="d1", role="writing-reviewer", task="Review post.md"):
        return reply(tool_calls=[(cid, "Delegate", {"role": role, "task": task})])

    def tools_offered(self, request_index):
        return [t["function"]["name"] for t in self.api.chat_requests()[request_index].get("tools", [])]


class DelegationTest(DelegationBase):
    # ---- tests ----------------------------------------------------------------------------------------
    def test_manual_mode_offers_no_delegate(self):
        self.orchestrate("manual")
        self.api.script.append(reply("hi"))
        aid = self.start("hello")
        self.wait_idle(aid)
        self.assertNotIn("Delegate", self.tools_offered(0))

    def test_the_coordinator_delegates_and_gets_the_answer(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(), reply("The text has one typo: 'erro'."), reply("Done: the reviewer found one typo.")]
        aid = self.start("review my post")
        self.wait_idle(aid)
        reqs = self.api.chat_requests()
        self.assertIn("Delegate", self.tools_offered(0))
        self.assertIn("writing-reviewer", reqs[0]["messages"][0]["content"])      # the roles it can use, with their AI
        self.assertIn("OtherAI", reqs[0]["messages"][0]["content"])
        self.assertEqual(reqs[1]["model"], "m2")                                  # the child runs on its own AI
        self.assertIn("Review post.md", reqs[1]["messages"][-1]["content"])
        self.assertNotIn("Delegate", self.tools_offered(1))                       # coordinator mode: children do not delegate
        self.assertIn("one typo", reqs[2]["messages"][-1]["content"])             # the child's answer came back
        self.assertIn(("text", "Done: the reviewer found one typo."), self.kinds(aid))
        tools = [p["name"] for k, p in self.kinds(aid) if k == "tool"]
        self.assertIn("Delegate", tools)
        hooks = [p for k, p in self.kinds(aid) if k == "hook" and p.get("aid")]
        self.assertEqual([h["ev"] for h in hooks][0], "SubagentStart")
        self.assertEqual([h["ev"] for h in hooks][-1], "SubagentStop")
        self.assertIn("→", hooks[0]["atype"])

    def test_delegation_asks_first_and_a_denial_reaches_the_parent(self):
        self.orchestrate()
        self.api.script += [self.delegate_call(), reply("ok, I will do it myself")]
        aid = self.start("review")
        req = self.wait_for("permission")
        self.assertEqual(req["tool"], "Delegate")
        self.assertIn("Review post.md", req["detail"])
        self.runner.resolve(req["req"], False, "Not now.")
        self.wait_idle(aid)
        self.assertEqual(len(self.api.chat_requests()), 2)                         # no child ever ran
        self.assertIn("Not now.", self.api.chat_requests()[1]["messages"][-1]["content"])

    def test_child_actions_ask_with_its_label(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(role="general", task="Fix post.md"),
                            reply(tool_calls=[("w1", "Write", {"file_path": "post.md", "content": "# Post\nFixed.\n"})]),
                            reply("fixed"), reply("all done")]
        aid = self.start("fix it")
        req = self.wait_for("permission")
        self.assertTrue(req["tool"].startswith("General assistant · FakeAI"), req["tool"])
        self.runner.resolve(req["req"], True)
        self.wait_idle(aid)
        self.assertEqual((Path(self.cwd) / "post.md").read_text(encoding="utf-8"), "# Post\nFixed.\n")
        self.assertIn(("general", "fake"), self.resolved)                         # no binding: the parent's AI

    def test_the_budget_is_shared_by_one_message(self):
        self.orchestrate(ask=False, max_delegations=1)
        self.api.script += [reply(tool_calls=[("d1", "Delegate", {"role": "architect", "task": "a"}),
                                              ("d2", "Delegate", {"role": "architect", "task": "b"})]),
                            reply("first"), reply("summary")]
        aid = self.start("two things")
        self.wait_idle(aid)
        last = self.api.chat_requests()[-1]["messages"]
        results = [m["content"] for m in last if m["role"] == "tool"]
        self.assertIn("first", results[0])
        self.assertTrue(results[1].startswith("Error:") and "limit" in results[1], results[1])
        self.api.script += [self.delegate_call(role="architect"), reply("again"), reply("ok")]
        self.runner.send(aid, "once more")                                         # a new message: a new budget
        self.wait_idle(aid, 2)
        self.assertIn("again", self.api.chat_requests()[-1]["messages"][-1]["content"])

    def test_unknown_or_forbidden_roles_are_refused(self):
        self.orchestrate(ask=False)
        self.api.script += [reply(tool_calls=[("d1", "Delegate", {"role": "coordinator", "task": "x"}),
                                              ("d2", "Delegate", {"role": "nobody", "task": "x"})]), reply("ok")]
        aid = self.start("go")
        self.wait_idle(aid)
        results = [m["content"] for m in self.api.chat_requests()[-1]["messages"] if m["role"] == "tool"]
        self.assertTrue(all(r.startswith("Error:") for r in results), results)

    def test_collaborative_depth_limit(self):
        self.orchestrate("collaborative", ask=False, max_depth=1)
        self.api.script += [reply(tool_calls=[("d1", "Delegate", {"role": "architect", "task": "plan"})]),
                            reply("plan ready"), reply("done")]
        aid = self.start("go", role_id="general")
        self.wait_idle(aid)
        self.assertIn("Delegate", self.tools_offered(0))
        self.assertNotIn("Delegate", self.tools_offered(1))                        # depth 1 = the limit

    def test_a_failing_child_returns_the_error_to_the_parent(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(), (429, {"error": {"message": "Quota exceeded"}}, {}), reply("sorry")]
        aid = self.start("review")
        self.wait_idle(aid)
        result = [m for m in self.api.chat_requests()[-1]["messages"] if m["role"] == "tool"][-1]["content"]
        self.assertTrue(result.startswith("Error:") and "Quota exceeded" in result, result)

    def test_a_child_that_fails_after_talking_is_a_failure(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(),
                            reply("Let me read post.md first.", tool_calls=[("r1", "Read", {"file_path": "post.md"})]),
                            (401, {"error": {"message": "bad key"}}, {}), reply("sorry")]
        aid = self.start("review")
        self.wait_idle(aid)
        result = [m for m in self.api.chat_requests()[-1]["messages"] if m["role"] == "tool"][-1]["content"]
        self.assertTrue(result.startswith("Error:") and "bad key" in result, result)

    def test_the_delegation_prompt_names_the_role_and_its_ai(self):
        self.orchestrate()
        self.api.script += [self.delegate_call(), reply("x"), reply("y")]
        aid = self.start("review")
        req = self.wait_for("permission")
        self.assertIn("Writing reviewer", req["detail"])
        self.assertIn("OtherAI", req["detail"])
        self.runner.resolve(req["req"], False)
        self.wait_idle(aid)

    def test_turning_asking_back_on_takes_effect_at_the_next_message(self):
        self.orchestrate(ask=False)
        self.api.script += [reply("hi")]
        aid = self.start("hello")
        self.wait_idle(aid)
        self.orchestrate(ask=True)
        self.api.script += [self.delegate_call(), reply("done")]
        self.runner.send(aid, "now delegate")
        req = self.wait_for("permission")
        self.assertEqual(req["tool"], "Delegate")
        self.runner.resolve(req["req"], False)
        self.wait_idle(aid, 2)

    def test_switching_to_manual_stops_delegation_of_a_live_agent(self):
        self.orchestrate(ask=False)
        self.api.script += [reply("hi")]
        aid = self.start("hello")
        self.wait_idle(aid)
        self.orchestrate("manual")
        self.api.script += [self.delegate_call(), reply("ok")]
        self.runner.send(aid, "delegate please")
        self.wait_idle(aid, 2)
        result = [m for m in self.api.chat_requests()[-1]["messages"] if m["role"] == "tool"][-1]["content"]
        self.assertTrue(result.startswith("Error:"), result)

    def test_interrupting_the_parent_withdraws_the_childs_request(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(role="general", task="Fix post.md"),
                            reply(tool_calls=[("w1", "Write", {"file_path": "post.md", "content": "x"})]),
                            reply("after")]
        aid = self.start("fix")
        req = self.wait_for("permission")
        self.runner.interrupt(aid)
        withdrawn = self.wait_for("permission_withdrawn")
        self.assertEqual(withdrawn["req"], req["req"])
        self.assertIn(("permission_withdrawn", withdrawn), self.kinds(aid))

    def test_interrupting_the_parent_cancels_the_child(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(role="general", task="Fix post.md"),
                            reply(tool_calls=[("w1", "Write", {"file_path": "post.md", "content": "x"})]),
                            reply("after")]
        aid = self.start("fix")
        req = self.wait_for("permission")
        self.runner.interrupt(aid)
        self.wait_idle(aid)
        self.runner.resolve(req["req"], True)                                      # too late: nothing is written
        time.sleep(0.3)
        self.assertEqual((Path(self.cwd) / "post.md").read_text(encoding="utf-8"), "# Post\nUm texto com erro.\n")
        self.runner.send(aid, "next")
        self.wait_for("text", lambda p: p == "after")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(managed.sdk is None, "claude-agent-sdk not installed")
class ClaudeDelegationTest(unittest.TestCase):
    def setUp(self):
        self.runner = managed.Runner(lambda *a: None, client_factory=lambda options: None)
        self.addCleanup(self.runner.shutdown)
        self.cwd = tempfile.mkdtemp()

    def orchestrate(self, mode, ask=True):
        st = orchestration.settings({"orchestration": {"mode": mode, "ask": ask}})
        self.runner.orchestration = {"settings": st, "roles": ROLES, "resolve": None, "conn_name": lambda r: "Claude"}

    def agent(self, role_id):
        a = managed._Agent("a1", ROLES[role_id], self.cwd, "", None)
        self.runner._prepare(a)
        return a

    def test_only_an_agent_that_may_delegate_gets_the_tool_and_loses_native_subagents(self):
        self.orchestrate("manual")
        options = self.runner._options(self.agent("coordinator"))
        self.assertFalse(options.mcp_servers)
        self.assertNotIn("Task", options.disallowed_tools or [])
        self.orchestrate("coordinator")
        options = self.runner._options(self.agent("coordinator"))
        self.assertIn("nina", options.mcp_servers)
        self.assertTrue({"Task", "Agent"} <= set(options.disallowed_tools))
        self.assertFalse(self.runner._options(self.agent("general")).mcp_servers)   # coordinator mode: only it

    def test_the_tool_runs_the_delegation_and_returns_its_answer(self):
        import asyncio
        self.orchestrate("coordinator")
        agent = self.agent("coordinator")
        seen = []

        async def fake_delegate(parent, role, task):
            seen.append((parent.id, role, task))
            return "the answer"

        self.runner.delegate = fake_delegate
        tool = self.runner._delegate_tool(agent)
        result = asyncio.run(tool.handler({"role": "writing-reviewer", "task": "check"}))
        self.assertEqual(seen, [("a1", "writing-reviewer", "check")])
        self.assertEqual(result["content"][0]["text"], "the answer")
        self.assertFalse(result.get("is_error"))

    def test_the_gate_treats_the_claude_tool_as_delegate(self):
        import asyncio
        from nina.agents.permissions import Gate
        events = []

        async def check(role):
            gate = Gate(asyncio.get_running_loop(), lambda *a: events.append(a), lambda: None, lambda: [])
            task = asyncio.ensure_future(gate.check("a", role, self.cwd, "mcp__nina__delegate",
                                                    {"role": "writing-reviewer", "task": "t"}, set()))
            for _ in range(20):
                await asyncio.sleep(0)
            if not task.done():
                gate.resolve(events[-1][2]["req"], True)
            return await task

        self.assertEqual(asyncio.run(check(ROLES["coordinator"])), (True, ""))
        self.assertEqual(events[-1][2]["tool"], "Delegate")                     # it asked, as Delegate
        events.clear()
        approved = {**ROLES["coordinator"], "tools": ROLES["coordinator"]["tools"] + ["Delegate"]}
        self.assertEqual(asyncio.run(check(approved)), (True, ""))
        self.assertEqual(events, [])                                            # pre-approved: no question

    def test_a_claude_child_reports_back_through_the_runner(self):
        """A delegated Claude agent's events must go through the interceptor, or the delegation never ends."""
        import asyncio
        sdk = managed.sdk
        owner = self

        class FakeClient:
            def __init__(self, options):
                owner.child_options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def query(self, text):
                pass

            async def interrupt(self):
                pass

            async def get_context_usage(self):
                raise RuntimeError("n/a")

            async def receive_response(self):
                yield sdk.AssistantMessage(content=[sdk.TextBlock(text="claude child answer")], model="m")
                yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                        num_turns=1, session_id="s-child", total_cost_usd=0.01)

        runner = managed.Runner(lambda *a: None, client_factory=FakeClient)
        self.addCleanup(runner.shutdown)
        st = orchestration.settings({"orchestration": {"mode": "coordinator", "ask": False}})
        claude = {"id": "claude", "kind": "claude", "name": "Claude"}
        runner.orchestration = {"settings": st, "roles": ROLES, "resolve": lambda role, conn: (claude, "", "", None),
                                "conn_name": lambda r: "Claude"}
        parent = managed._Agent("p1", ROLES["coordinator"], self.cwd, "", None, claude)
        runner._prepare(parent)
        future = asyncio.run_coroutine_threadsafe(runner.delegate(parent, "writing-reviewer", "check"), runner.loop)
        self.assertEqual(future.result(timeout=10), "claude child answer")


class DelegateDisplayTest(unittest.TestCase):
    def test_the_chat_line_names_the_role_and_the_task(self):
        from nina.agents import openai_agent
        self.assertEqual(managed.tool_summary({"role": "writing-reviewer", "task": "Check post.md"}),
                         "writing-reviewer: Check post.md")
        self.assertEqual(openai_agent._summary({"role": "general", "task": "Write resumo.md"}), "general: Write resumo.md")
        self.assertEqual(managed.display_tool("mcp__nina__delegate"), "Delegate")
        self.assertEqual(managed.display_tool("Read"), "Read")


class StoppedChildTest(unittest.TestCase):
    def test_a_stopped_agent_never_sends_another_request(self):
        """An interrupted child must not spend one more API call, even if a denied question wakes it first."""
        import asyncio
        from nina.agents import openai_agent
        api = FakeOpenAI()
        self.addCleanup(api.close)
        cwd = tempfile.mkdtemp()
        conn = {"id": "f", "kind": "openai", "name": "F", "base_url": api.base_url, "model": "m",
                "key": {"source": "none"}, "timeout_s": 10, "headers": {}, "context_window": 0,
                "price_in": 0.0, "price_out": 0.0}
        agent = managed._Agent("c1", ROLES["general"], cwd, "", None, conn, "m")
        agent.stopped = True
        api.script.append(reply("should never be asked"))
        bot = openai_agent.OpenAIAgent(agent, conn, "m", gate=None, emit=lambda *a: None)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(bot._turn("hi"))
        self.assertEqual(api.chat_requests(), [])


class DeferredFixesTest(DelegationBase):
    """The minors deferred at the end of Project 2."""

    def test_only_the_childs_final_answer_comes_back(self):
        self.orchestrate(ask=False)
        self.api.script += [self.delegate_call(role="architect", task="plan"),
                            reply("Let me look at the files first.", tool_calls=[("g1", "Glob", {"pattern": "*.md"})]),
                            reply("Final plan: one step."), reply("done")]
        aid = self.start("plan it")
        self.wait_idle(aid)
        result = [m for m in self.api.chat_requests()[-1]["messages"] if m["role"] == "tool"][-1]["content"]
        self.assertEqual(result, "Final plan: one step.")

    def test_delegated_cost_reaches_the_parent_totals(self):
        self.orchestrate(ask=False)
        self.other = {**self.other, "price_in": 1000.0, "price_out": 0.0}
        self.api.script += [self.delegate_call(), reply("reviewed", usage=(100, 0)), reply("done")]
        aid = self.start("review")
        self.wait_idle(aid)
        extra = [p for k, p in self.kinds(aid) if k == "extra_cost"]
        self.assertEqual(len(extra), 1)
        self.assertAlmostEqual(extra[0], 100 * 1000.0 / 1e6)

    def test_a_child_names_the_ai_its_own_children_would_use(self):
        self.orchestrate("collaborative", ask=False, max_depth=2)
        # the writing reviewer runs on OtherAI; architect has no binding, so from there it would run on OtherAI too
        self.api.script += [self.delegate_call(role="writing-reviewer", task="review"), reply("ok"), reply("done")]
        aid = self.start("go", role_id="general")
        self.wait_idle(aid)
        child_system = self.api.chat_requests()[1]["messages"][0]["content"]
        line = next(l for l in child_system.splitlines() if l.startswith("- architect"))
        self.assertIn("OtherAI", line)

    def test_delegation_prompts_are_not_translated(self):
        from nina import i18n
        i18n.set_language("pt-BR")
        self.addCleanup(i18n.set_language, "en")
        text = orchestration.delegation_prompt(["writing-reviewer"], ROLES, lambda r: "Gemini")
        self.assertIn("Writing reviewer", text)
        self.assertNotIn("Revisor", text)
