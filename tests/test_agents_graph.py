import unittest

from nina.agents import graph
from nina.agents.store import AgentStore


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.s = AgentStore(":memory:")
        self.addCleanup(self.s.close)
        self.t0 = 2_000_000.0
        rec = lambda name, dt, **k: self.s.record({"ev": name, "sid": k.pop("sid", "S1"), **k}, now=self.t0 + dt)  # noqa: E731
        rec("UserPromptSubmit", 0, prompt="Fix IPI")
        rec("PreToolUse", 1, tool="Read", detail="/p/app/Ipi.php")
        rec("PreToolUse", 2, tool="Edit", detail="/p/app/Ipi.php")
        rec("PreToolUse", 3, tool="Bash", detail="php artisan test")
        rec("PreToolUse", 4, tool="mcp__tracker__get_task", detail="DEV-1")
        rec("SubagentStart", 5, aid="A1", atype="Explore")
        rec("PreToolUse", 6, aid="A1", atype="Explore", tool="Grep", detail="calculateIpi")
        rec("SubagentStop", 7, aid="A1", atype="Explore", last="ok")
        rec("UserPromptSubmit", 8, sid="S2", prompt="Another task")
        rec("Stop", 9, last="done")

    def graph(self, dt):
        return graph.build_graph(self.s.all_agents(), self.s.all_events(), self.t0 + dt)

    def test_nodes_and_edges(self):
        gr = self.graph(20)
        n, e = gr["nodes"], gr["edges"]
        for expected in ("nina", "S1", "S2", "S1/A1", "tool:Bash", "tool:Edit", "tool:Grep", "mcp:tracker", "file:/p/app/Ipi.php"):
            self.assertIn(expected, n)
        self.assertEqual(e[("nina", "S1")]["kind"], "session")
        self.assertEqual(e[("S1", "S1/A1")]["kind"], "spawn")
        self.assertEqual(e[("S1", "file:/p/app/Ipi.php")]["kind"], "write")   # read and wrote: write wins
        self.assertEqual(n["file:/p/app/Ipi.php"]["label"], "Ipi.php")
        self.assertEqual((n["S1"]["status"], n["S1/A1"]["status"], n["S2"]["status"]), ("done", "done", "working"))

    def test_replay_in_time(self):
        gr = self.graph(2.5)
        self.assertNotIn("tool:Bash", gr["nodes"])
        self.assertNotIn("S2", gr["nodes"])
        self.assertEqual(gr["nodes"]["S1"]["status"], "working")
        self.assertNotIn("S1/A1", gr["nodes"])

    def test_no_signal_depends_on_the_instant(self):
        self.assertEqual(self.graph(8 + graph.STALE_S + 5)["nodes"]["S2"]["status"], "stale")

    def test_pulses(self):
        p = graph.pulses_for
        self.assertEqual(p("S1", "prompt", None, None, None), [("nina", "S1")])
        self.assertEqual(p("S1", "tool", "Edit", "/a/b.py", None), [("S1", "tool:Edit"), ("S1", "file:/a/b.py")])
        self.assertEqual(p("S1/A1", "start", None, None, "S1"), [("S1", "S1/A1")])
        self.assertEqual(p("S1/A1", "done", None, None, "S1"), [("S1/A1", "S1")])
        self.assertEqual(p("S1", "tool", "mcp__x__y", None, None), [("S1", "mcp:x")])

    def test_layout_settles_without_overlap(self):
        gr = self.graph(20)
        lay = graph.ForceLayout()
        self.assertTrue(lay.update(gr["nodes"], gr["edges"]))
        lay.settle(gr["edges"], 400)
        self.assertEqual(lay.pos["nina"], [0.0, 0.0])
        pts = list(lay.pos.values())
        self.assertTrue(all(abs(x) < 700 and abs(y) < 700 for x, y in pts))   # compact, it does not "explode"
        dmin = min(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for i, a in enumerate(pts) for b in pts[i + 1:])
        self.assertGreater(dmin, 18)
        self.assertFalse(lay.update(gr["nodes"], gr["edges"]))   # nothing changed
        small = {k: v for k, v in gr["nodes"].items() if k in ("nina", "S1")}
        self.assertTrue(lay.update(small, {}))                    # nodes removed
        self.assertEqual(set(lay.pos), {"nina", "S1"})


if __name__ == "__main__":
    unittest.main()
