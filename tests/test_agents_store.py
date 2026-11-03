import unittest

from nina.agents.store import STALE_AFTER_S, AgentStore, redact


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = AgentStore(":memory:")
        self.addCleanup(self.s.close)
        self.t0 = 1_000_000.0

    def ev(self, name, now, **kw):
        self.s.record({"ev": name, "sid": "S1", **kw}, now=now)

    def test_session_lifecycle(self):
        self.ev("SessionStart", self.t0, cwd="/p/x", tp="/t/S1.jsonl")
        self.ev("UserPromptSubmit", self.t0 + 1, prompt="Fix the login bug")
        self.ev("PreToolUse", self.t0 + 2, tool="Edit", detail="/p/x/a.py")
        (a,) = self.s.list_agents(self.t0 + 3)
        self.assertEqual((a["display"], a["title"], a["activity"]), ("working", "Fix the login bug", "Edit: /p/x/a.py"))
        self.assertEqual(self.s.active_count(self.t0 + 3), 1)
        self.ev("Notification", self.t0 + 4, msg="Claude needs your permission")
        self.assertEqual(self.s.list_agents(self.t0 + 5)[0]["display"], "waiting")
        self.ev("PreToolUse", self.t0 + 6, tool="Bash", detail="ls")
        self.ev("Stop", self.t0 + 7, last="Done, fixed.")
        a = self.s.list_agents(self.t0 + 8)[0]
        self.assertEqual((a["display"], a["activity"]), ("done", "Done: Done, fixed."))
        self.assertEqual(self.s.active_count(self.t0 + 8), 0)
        self.ev("SessionEnd", self.t0 + 9, reason="other")
        self.assertEqual(self.s.list_agents(self.t0 + 10)[0]["display"], "ended")

    def test_new_prompt_reopens_and_keeps_the_title(self):
        self.ev("UserPromptSubmit", self.t0, prompt="First")
        self.ev("Stop", self.t0 + 1)
        self.ev("UserPromptSubmit", self.t0 + 2, prompt="Second")
        a = self.s.list_agents(self.t0 + 3)[0]
        self.assertEqual((a["display"], a["title"]), ("working", "First"))

    def test_subagents_become_children(self):
        self.ev("UserPromptSubmit", self.t0, prompt="Investigate")
        self.ev("SubagentStart", self.t0 + 1, aid="A1", atype="Explore")
        self.ev("PreToolUse", self.t0 + 2, aid="A1", atype="Explore", tool="Grep", detail="foo")
        (a,) = self.s.list_agents(self.t0 + 3)
        (c,) = a["children"]
        self.assertEqual((c["id"], c["agent_type"], c["display"], c["activity"]), ("S1/A1", "Explore", "working", "Grep: foo"))
        self.assertEqual(self.s.active_count(self.t0 + 3), 2)
        self.ev("SubagentStop", self.t0 + 4, aid="A1", atype="Explore", last="found it")
        self.assertEqual(self.s.list_agents(self.t0 + 5)[0]["children"][0]["display"], "done")

    def test_no_signal(self):
        self.ev("PreToolUse", self.t0, tool="Bash", detail="sleep 999")
        self.assertEqual(self.s.list_agents(self.t0 + STALE_AFTER_S + 1)[0]["display"], "stale")
        self.assertEqual(self.s.active_count(self.t0 + STALE_AFTER_S + 1), 0)

    def test_list_priority_and_cleanup(self):
        self.s.record({"ev": "UserPromptSubmit", "sid": "A", "prompt": "a"}, now=self.t0)
        self.s.record({"ev": "SessionEnd", "sid": "A"}, now=self.t0 + 1)
        self.s.record({"ev": "UserPromptSubmit", "sid": "B", "prompt": "b"}, now=self.t0 + 2)
        self.assertEqual([x["id"] for x in self.s.list_agents(self.t0 + 3)], ["B", "A"])
        self.s.clear_finished()
        self.assertEqual([x["id"] for x in self.s.list_agents(self.t0 + 3)], ["B"])
        self.assertEqual(self.s.events("A"), [])

    def test_invalid_events_are_ignored(self):
        self.s.record({}, now=self.t0)
        self.s.record({"ev": "Stop"}, now=self.t0)
        self.assertEqual(self.s.list_agents(self.t0), [])

    def test_secrets_are_masked(self):
        fake_key = "sk-" + "abcdefghijklmnopqrstuv"   # assembled at runtime so secret scanners ignore this fixture
        self.ev("PreToolUse", self.t0, tool="Bash", detail="curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://x")
        self.ev("PreToolUse", self.t0 + 1, tool="Bash", detail=f"export API_KEY=supersecretvalue123 {fake_key}")
        texts = " ".join(e["summary"] for e in self.s.events("S1"))
        for leaked in ("abcdefghijklmnop1234", "supersecretvalue123", fake_key):
            self.assertNotIn(leaked, texts)
        self.assertIn("[hidden]", redact("password=hunter2hunter2"))


if __name__ == "__main__":
    unittest.main()
