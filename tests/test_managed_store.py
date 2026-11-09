import json
import os
import tempfile
import unittest
from pathlib import Path

from nina.agents import managed_store


class ManagedStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "managed.json"

    def agent(self, **kw):
        return {"role": "general", "cwd": "/work", "account": "a", "session": "s1", "cost": 0.5,
                "log": [("user", "hi"), ("assistant", "hello")], **kw}

    def test_round_trip(self):
        managed_store.save([self.agent()], self.path)
        self.assertEqual(managed_store.load(self.path), [self.agent()])

    def test_file_is_private(self):
        managed_store.save([self.agent()], self.path)
        if os.name == "posix":   # on Windows the user profile's ACL keeps it private
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_agents_without_a_session_are_not_saved(self):
        managed_store.save([self.agent(session=None), self.agent(session="s2")], self.path)
        self.assertEqual([a["session"] for a in managed_store.load(self.path)], ["s2"])

    def test_missing_or_damaged_file_gives_empty_list(self):
        self.assertEqual(managed_store.load(self.path), [])
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(managed_store.load(self.path), [])
        self.path.write_text(json.dumps({"agents": [{"role": "x"}, 5, {"role": "g", "cwd": "/w", "session": "s", "log": [1, ["a"], ["k", "t"]]}]}), encoding="utf-8")
        loaded = managed_store.load(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["log"], [("k", "t")])

    def test_limits(self):
        many = [self.agent(session=f"s{i}", log=[("info", str(j)) for j in range(500)]) for i in range(50)]
        managed_store.save(many, self.path)
        loaded = managed_store.load(self.path)
        self.assertEqual(len(loaded), managed_store.MAX_AGENTS)
        self.assertEqual(len(loaded[0]["log"]), managed_store.MAX_LOG)


if __name__ == "__main__":
    unittest.main()
