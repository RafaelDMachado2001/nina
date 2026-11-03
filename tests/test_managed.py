import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from nina.agents import managed, roles


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)
        self.builder = roles.load_roles()["agent-builder"]
        self.reviewer = roles.load_roles()["code-reviewer"]

    def test_reads_are_pre_approved(self):
        self.assertEqual(managed.decide("Read", {"file_path": "x"}, self.reviewer, self.cwd)[0], "allow")
        self.assertEqual(managed.decide("Grep", {"pattern": "x"}, self.builder, self.cwd)[0], "allow")

    def test_writes_only_in_the_roles_paths(self):
        ok = managed.decide("Write", {"file_path": f"{self.cwd}/.claude/agents/new.md", "content": "x"}, self.builder, self.cwd)
        self.assertEqual(ok[0], "ask")                               # allowed, but still asks
        self.assertEqual(managed.decide("Write", {"file_path": ".claude/agents/new.md"}, self.builder, self.cwd)[0], "ask")
        for bad in ("src/app.py", "README.md", ".claude/settings.json"):
            self.assertEqual(managed.decide("Write", {"file_path": bad}, self.builder, self.cwd)[0], "deny", bad)

    def test_cannot_leave_the_folder(self):
        for bad in ("/etc/passwd", "../outside.md", ".claude/agents/../../../x.md", f"{self.cwd}/../other/.claude/agents/a.md"):
            self.assertEqual(managed.decide("Write", {"file_path": bad}, self.builder, self.cwd)[0], "deny", bad)

    def test_symlink_does_not_escape(self):
        outside = tempfile.mkdtemp()
        os.makedirs(f"{self.cwd}/.claude")
        os.symlink(outside, f"{self.cwd}/.claude/agents")
        self.assertEqual(managed.decide("Write", {"file_path": ".claude/agents/x.md"}, self.builder, self.cwd)[0], "deny")

    def test_read_only_role_never_writes(self):
        self.assertEqual(managed.decide("Write", {"file_path": "a.md"}, self.reviewer, self.cwd)[0], "deny")

    def test_bash_asks_for_confirmation(self):
        self.assertEqual(managed.decide("Bash", {"command": "ls"}, self.reviewer, self.cwd)[0], "ask")

    def test_description_masks_secrets_and_is_limited(self):
        text = managed.describe("Bash", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234' x"})
        self.assertNotIn("abcdefghijklmnop1234", text)
        long = managed.describe("Write", {"file_path": "a.md", "content": "\n".join(str(i) for i in range(100))})
        self.assertIn("(+76 lines)", long)


class RolesTest(unittest.TestCase):
    def test_builtin_roles(self):
        r = roles.load_roles()
        for rid in ("general", "agent-builder", "code-reviewer", "writing-reviewer", "architect", "project-manager", "product-owner"):
            self.assertIn(rid, r)
            self.assertTrue(r[rid]["prompt"] and r[rid]["tools"])
        self.assertEqual(r["writing-reviewer"]["writes"], [])

    def test_role_file_overrides_and_invalid_ones_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "product-owner.md").write_text("---\ntitle: My PO\ntools: Read, Bash\nwrites: docs/*, notes/*\nmodel: sonnet\n---\nBe brief.")
            Path(d, "broken.md").write_text("no frontmatter")
            Path(d, "empty.md").write_text("---\ntitle: x\n---\n")
            r = roles.load_roles(d)
        po = r["product-owner"]
        self.assertEqual((po["title"], po["tools"], po["writes"], po["model"]), ("My PO", ["Read", "Bash"], ["docs/*", "notes/*"], "sonnet"))
        self.assertNotIn("broken", r)
        self.assertNotIn("empty", r)


@unittest.skipUnless(managed.available(), "claude-agent-sdk is not installed")
class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.events, self.lock = [], threading.Lock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)
        self.sdk = managed.sdk

    def tearDown(self):
        self.runner.shutdown()

    def emit(self, aid, kind, payload):
        with self.lock:
            self.events.append((aid, kind, payload))

    def kinds(self):
        with self.lock:
            return [(k, p) for _a, k, p in self.events]

    def wait_for(self, kind, pred=lambda p: True, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for k, p in self.kinds():
                if k == kind and pred(p):
                    return p
            time.sleep(0.01)
        self.fail(f"event {kind} never arrived; received: {self.kinds()}")

    def make(self, script):
        owner = self

        class FakeClient:
            def __init__(self, options):
                self.options, self.queries, self.interrupted = options, [], False
                owner.client = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def query(self, text):
                self.queries.append(text)

            async def interrupt(self):
                self.interrupted = True

            async def receive_response(self):
                async for m in script(self):
                    yield m

        self.runner = managed.Runner(self.emit, client_factory=FakeClient)

    def result(self, sid="sess-1"):
        return self.sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                      num_turns=1, session_id=sid, total_cost_usd=0.0123)

    def test_basic_conversation_and_follow_up(self):
        sdk = self.sdk

        async def script(client):
            yield sdk.SystemMessage(subtype="init", data={"session_id": "sess-1"})
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text=f"reply {len(client.queries)} token=abcdef123456"),
                                                sdk.ToolUseBlock(id="t", name="Read", input={"file_path": "a.py"})], model="m")
            yield self.result()

        self.make(script)
        aid = self.runner.start(roles.load_roles()["code-reviewer"], self.cwd, "hello")
        self.assertEqual(self.wait_for("session"), "sess-1")
        self.assertEqual(self.wait_for("text", lambda p: "reply 1" in p).count("[hidden]"), 1)   # secret masked
        self.assertEqual(self.wait_for("tool"), {"name": "Read", "summary": "a.py"})
        self.assertEqual(self.wait_for("result")["cost"], 0.0123)
        self.wait_for("status", lambda p: p == "idle")
        self.runner.send(aid, "and now?")
        self.wait_for("text", lambda p: "reply 2" in p)
        self.assertEqual(self.client.queries, ["hello", "and now?"])
        self.runner.stop(aid)
        self.wait_for("status", lambda p: p == "closed")

    def test_role_options(self):
        async def script(client):
            yield self.result()

        self.make(script)
        self.runner.start(roles.load_roles()["code-reviewer"], self.cwd, "x", account="personal", config_dir="/cfg/personal")
        self.wait_for("result")
        o = self.client.options
        self.assertEqual(o.allowed_tools, ["Read", "Glob", "Grep"])
        self.assertEqual(sorted(o.disallowed_tools), ["Edit", "MultiEdit", "NotebookEdit", "Write"])   # read-only role
        self.assertEqual(o.env, {"CLAUDE_CONFIG_DIR": "/cfg/personal", "CLAUDE_ACCOUNT": "personal"})
        self.assertEqual(o.cwd, self.cwd)
        self.assertIn("code reviewer", o.system_prompt["append"])

    def test_permission_allowed_denied_and_policy(self):
        sdk = self.sdk
        outcomes = {}

        async def script(client):
            can = client.options.can_use_tool
            outcomes["bash"] = await can("Bash", {"command": "ls"}, None)
            outcomes["write_ok"] = await can("Write", {"file_path": ".claude/agents/a.md", "content": "x"}, None)
            outcomes["write_bad"] = await can("Write", {"file_path": "src/x.py", "content": "x"}, None)   # denied without asking
            yield self.result()

        self.make(script)
        self.runner.start(roles.load_roles()["agent-builder"], self.cwd, "x")
        p1 = self.wait_for("permission", lambda p: p["tool"] == "Bash")
        self.runner.resolve(p1["req"], True)
        p2 = self.wait_for("permission", lambda p: p["tool"] == "Write")
        self.assertIn(".claude/agents/a.md", p2["detail"])
        self.runner.resolve(p2["req"], False, "no thanks")
        self.wait_for("result")
        self.assertIsInstance(outcomes["bash"], sdk.PermissionResultAllow)
        self.assertIsInstance(outcomes["write_ok"], sdk.PermissionResultDeny)
        self.assertEqual(outcomes["write_ok"].message, "no thanks")
        self.assertIsInstance(outcomes["write_bad"], sdk.PermissionResultDeny)
        self.assertEqual(self.wait_for("denied")["tool"], "Write")
        self.assertEqual(len([k for k, _ in self.kinds() if k == "permission"]), 2)   # the policy denial never asked

    def test_interrupt(self):
        async def script(client):
            await asyncio.sleep(0.2)
            yield self.result()

        self.make(script)
        aid = self.runner.start(roles.load_roles()["general"], self.cwd, "x")
        self.wait_for("status", lambda p: p == "working")
        self.runner.interrupt(aid)
        t0 = time.time()
        while not getattr(self.client, "interrupted", False) and time.time() - t0 < 3:
            time.sleep(0.01)
        self.assertTrue(self.client.interrupted)

    def test_closing_with_a_pending_permission_denies_it(self):
        outcome = {}

        async def script(client):
            outcome["r"] = await client.options.can_use_tool("Bash", {"command": "ls"}, None)
            yield self.result()

        self.make(script)
        aid = self.runner.start(roles.load_roles()["general"], self.cwd, "x")
        self.wait_for("permission")
        self.runner.stop(aid)
        self.wait_for("result")
        self.assertIsInstance(outcome["r"], self.sdk.PermissionResultDeny)

    def test_client_error_becomes_an_event(self):
        async def script(client):
            raise RuntimeError("simulated failure")
            yield

        self.make(script)
        self.runner.start(roles.load_roles()["general"], self.cwd, "x")
        self.assertIn("simulated failure", self.wait_for("error"))
        self.wait_for("status", lambda p: p == "closed")


if __name__ == "__main__":
    unittest.main()
