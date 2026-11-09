import asyncio
import os
import tempfile
import unittest
from unittest import mock

from nina.agents import permissions
from nina.agents.permissions import Gate, decide, rule_matches

ROLE = {"tools": ["Read", "Glob", "Grep", "Bash(git log:*)"], "writes": ["docs/*"]}


class RuleTest(unittest.TestCase):
    def test_rule_syntax(self):
        self.assertTrue(rule_matches("Read", "Read", {"file_path": "x"}))
        self.assertFalse(rule_matches("Read", "Write", {}))
        self.assertTrue(rule_matches("Bash(git log:*)", "Bash", {"command": "git log --oneline"}))
        self.assertFalse(rule_matches("Bash(git log:*)", "Bash", {"command": "git push"}))
        self.assertTrue(rule_matches("Bash(ls)", "Bash", {"command": "ls"}))
        self.assertFalse(rule_matches("Bash(ls)", "Bash", {"command": "ls -la"}))
        self.assertTrue(rule_matches("Write(*.md)", "Write", {"file_path": "notes/a.md"}))
        self.assertTrue(rule_matches("WebFetch(https://example.com/*)", "WebFetch", {"url": "https://example.com/a"}))
        self.assertFalse(rule_matches("Bash(", "Bash", {"command": "x"}))           # malformed: never matches

    def test_role_rules_preapprove(self):
        with tempfile.TemporaryDirectory() as cwd:
            self.assertEqual(decide("Bash", {"command": "git log -3"}, ROLE, cwd)[0], "allow")
            self.assertEqual(decide("Bash", {"command": "rm x"}, ROLE, cwd)[0], "ask")

    def test_deny_list_wins_over_role_tools(self):
        with tempfile.TemporaryDirectory() as cwd:
            verdict, why = decide("Read", {"file_path": "a"}, ROLE, cwd, deny=["Read"])
            self.assertEqual(verdict, "deny")
            self.assertIn("Read", why)
            self.assertEqual(decide("Bash", {"command": "rm -rf /"}, ROLE, cwd, deny=["Bash(rm -rf:*)"])[0], "deny")

    def test_write_policy_is_unchanged(self):
        with tempfile.TemporaryDirectory() as cwd:
            cwd = os.path.realpath(cwd)
            self.assertEqual(decide("Write", {"file_path": "docs/a.md"}, ROLE, cwd)[0], "ask")
            self.assertEqual(decide("Write", {"file_path": "src/a.py"}, ROLE, cwd)[0], "deny")
            self.assertEqual(decide("Write", {"file_path": "../x.md"}, ROLE, cwd)[0], "deny")


class FakeStep:
    def __init__(self, label, hard=False):
        self.label, self.hard = label, hard


class FakeAuto:
    def __init__(self, on=True, pending=None):
        self.on, self._pending, self.saved = on, pending or [], []
        self.rules = self

    def enabled(self):
        return self.on

    def pending(self, command):
        return list(self._pending)

    def add(self, steps):
        self.saved.append([s.label for s in steps])


class GateTest(unittest.TestCase):
    def run_gate(self, tool, tool_input, answer=None, auto=None, deny=(), role=ROLE):
        events = []

        async def main():
            loop = asyncio.get_running_loop()
            gate = Gate(loop, lambda aid, kind, p: events.append((kind, p)), lambda: auto, lambda: list(deny))
            pending = set()
            task = asyncio.ensure_future(gate.check("a1", role, self.cwd, tool, tool_input, pending))
            for _ in range(50):
                await asyncio.sleep(0)
                asked = [p for k, p in events if k == "permission"]
                if asked and answer is not None:
                    gate.resolve(asked[0]["req"], *answer)
                    break
            result = await task
            return result, pending

        result, pending = asyncio.run(main())
        return result, events, pending

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)

    def test_gate_allows_preapproved_without_asking(self):
        (ok, _msg), events, _p = self.run_gate("Read", {"file_path": "x"})
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_gate_denies_by_policy_and_reports_it(self):
        (ok, msg), events, _p = self.run_gate("Write", {"file_path": "src/x.py", "content": ""})
        self.assertFalse(ok)
        self.assertEqual(events[0][0], "denied")
        self.assertIn("src", msg)

    def test_gate_asks_and_waits_for_resolve(self):
        (ok, _m), events, pending = self.run_gate("Write", {"file_path": "docs/a.md", "content": "hi"},
                                                 answer=(True, "", False))
        self.assertTrue(ok)
        self.assertEqual(events[0][0], "permission")
        self.assertIn("docs/a.md", events[0][1]["detail"])
        self.assertEqual(pending, set())                          # forgotten once answered
        (ok, msg), _e, _p = self.run_gate("Bash", {"command": "make"}, answer=(False, "no thanks", False))
        self.assertEqual((ok, msg), (False, "no thanks"))

    def test_gate_timeout_denies(self):
        with mock.patch.object(permissions, "PERMISSION_TIMEOUT_S", 0.05):
            (ok, msg), _e, _p = self.run_gate("Bash", {"command": "make"})
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_auto_mode_allows_read_only_bash(self):
        (ok, _m), events, _p = self.run_gate("Bash", {"command": "ls"}, auto=FakeAuto(pending=[]))
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_allow_always_saves_the_rule(self):
        auto = FakeAuto(pending=[FakeStep("ssh srv: UPDATE")])
        (ok, _m), events, _p = self.run_gate("Bash", {"command": "ssh srv psql -c 'update t set a=1 where id=2'"},
                                             answer=(True, "", True), auto=auto)
        self.assertTrue(ok)
        self.assertEqual(events[0][1]["remember"], ["ssh srv: UPDATE"])
        self.assertEqual(auto.saved, [["ssh srv: UPDATE"]])


if __name__ == "__main__":
    unittest.main()
