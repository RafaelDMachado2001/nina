import json
import tempfile
import unittest
from pathlib import Path

from nina.agents import transcript


class TranscriptTest(unittest.TestCase):
    def write(self, lines):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "s.jsonl"
        p.write_text("\n".join(lines), encoding="utf-8")
        return p

    def test_tolerant_tail(self):
        p = self.write([
            "this is not json",
            json.dumps({"type": "queue-operation"}),
            json.dumps({"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "Hi, help me"}}),
            json.dumps({"type": "assistant", "timestamp": "t2", "message": {"content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "Sure."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}}),
            json.dumps({"type": "user", "timestamp": "t3", "message": {"content": [
                {"type": "tool_result", "content": [{"type": "text", "text": "file.txt"}]}]}}),
            json.dumps({"type": "user", "isMeta": True, "message": {"content": "ignore"}}),
        ])
        got = [(m["role"], m["text"]) for m in transcript.tail_messages(p)]
        self.assertEqual(got, [("user", "Hi, help me"), ("assistant", "Sure."), ("tool", "Bash: ls -la"), ("result", "file.txt")])

    def test_automatic_title(self):
        p = self.write([json.dumps({"type": "ai-title", "aiTitle": "Old"}), json.dumps({"type": "user", "message": {"content": "x"}}),
                        json.dumps({"type": "ai-title", "aiTitle": "Current title"})])
        self.assertEqual(transcript.find_title(p), "Current title")
        self.assertEqual(transcript.find_title("/does/not/exist.jsonl"), "")

    def test_missing_file(self):
        self.assertEqual(transcript.tail_messages("/does/not/exist.jsonl"), [])

    def test_subagent_paths(self):
        jsonl, meta = transcript.subagent_files("/x/proj/abc.jsonl", "a1")
        self.assertEqual((jsonl, meta), (Path("/x/proj/abc/subagents/agent-a1.jsonl"), Path("/x/proj/abc/subagents/agent-a1.meta.json")))


if __name__ == "__main__":
    unittest.main()
