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


class DelegationTest(unittest.TestCase):
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
