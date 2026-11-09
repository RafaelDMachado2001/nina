import asyncio
import http.server
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from nina.agents import tools

BASH = tools.find_bash()


def run(name, args, cwd, bash=BASH):
    return asyncio.run(tools.run(name, args, cwd, bash))


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)
        root = Path(self.cwd)
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("import os\nprint('Hello')\nprint('bye')\n", encoding="utf-8")
        (root / "notes.md").write_text("# Title\nhello world\n", encoding="utf-8")
        (root / "node_modules" / "x").mkdir(parents=True)
        (root / "node_modules" / "x" / "hello.js").write_text("hello", encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    def test_read_numbers_lines_with_offset_and_limit(self):
        out = run("Read", {"file_path": "src/app.py"}, self.cwd)
        self.assertIn("     1\timport os", out)
        self.assertIn("     3\tprint('bye')", out)
        out = run("Read", {"file_path": os.path.join(self.cwd, "src", "app.py"), "offset": 2, "limit": 1}, self.cwd)
        self.assertEqual(out.strip(), "2\tprint('Hello')")

    def test_read_refuses_binary_and_missing_files(self):
        self.assertTrue(run("Read", {"file_path": "blob.bin"}, self.cwd).startswith("Error:"))
        self.assertTrue(run("Read", {"file_path": "nope.txt"}, self.cwd).startswith("Error:"))

    def test_write_creates_parents(self):
        out = run("Write", {"file_path": "docs/new/a.md", "content": "olá"}, self.cwd)
        self.assertFalse(out.startswith("Error:"), out)
        self.assertEqual((Path(self.cwd) / "docs" / "new" / "a.md").read_text(encoding="utf-8"), "olá")

    def test_edit_not_found_ambiguous_and_replace_all(self):
        self.assertIn("not found", run("Edit", {"file_path": "src/app.py", "old_string": "nope", "new_string": "x"}, self.cwd))
        self.assertIn("2 times", run("Edit", {"file_path": "src/app.py", "old_string": "print", "new_string": "log"}, self.cwd))
        run("Edit", {"file_path": "src/app.py", "old_string": "print", "new_string": "log", "replace_all": True}, self.cwd)
        self.assertEqual((Path(self.cwd) / "src" / "app.py").read_text(encoding="utf-8"), "import os\nlog('Hello')\nlog('bye')\n")
        run("Edit", {"file_path": "notes.md", "old_string": "hello world", "new_string": "hi"}, self.cwd)
        self.assertEqual((Path(self.cwd) / "notes.md").read_text(encoding="utf-8"), "# Title\nhi\n")

    def test_glob_orders_by_mtime_and_ignores_vendored(self):
        later = Path(self.cwd) / "src" / "later.py"
        later.write_text("x", encoding="utf-8")
        os.utime(later, (time.time() + 10, time.time() + 10))
        out = run("Glob", {"pattern": "**/*.py"}, self.cwd).splitlines()
        self.assertEqual(out, ["src/later.py", "src/app.py"])
        self.assertNotIn("node_modules", run("Glob", {"pattern": "**/*.js"}, self.cwd))

    def test_grep_with_glob_and_case(self):
        out = run("Grep", {"pattern": "hello", "case_insensitive": True}, self.cwd)
        self.assertIn("notes.md", out)
        self.assertIn("src/app.py", out)
        self.assertNotIn("node_modules", out)
        self.assertNotIn("app.py", run("Grep", {"pattern": "hello", "glob": "*.md"}, self.cwd))
        content = run("Grep", {"pattern": "Hello", "output_mode": "content"}, self.cwd)
        self.assertIn("src/app.py:2:print('Hello')", content)
        self.assertTrue(run("Grep", {"pattern": "("}, self.cwd).startswith("Error:"))

    @unittest.skipIf(BASH is None, "no bash here")
    def test_bash_output_exit_code_and_timeout(self):
        out = run("Bash", {"command": "echo hi; echo oops >&2; exit 3"}, self.cwd)
        self.assertIn("hi", out)
        self.assertIn("oops", out)
        self.assertIn("exit code 3", out)
        self.assertIn("notes.md", run("Bash", {"command": "ls"}, self.cwd))          # runs in the agent's folder
        t0 = time.monotonic()
        self.assertIn("timed out", run("Bash", {"command": "sleep 8", "timeout_s": 1}, self.cwd))
        self.assertLess(time.monotonic() - t0, 6)                                        # not the full 8 s

    def test_bash_without_a_shell_is_an_error(self):
        self.assertTrue(run("Bash", {"command": "ls"}, self.cwd, bash=None).startswith("Error:"))

    def test_webfetch_returns_text(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><head><style>x{}</style><script>var a;</script></head><body><h1>Hi</h1><p>there &amp; you</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        out = run("WebFetch", {"url": f"http://127.0.0.1:{server.server_address[1]}/"}, self.cwd)
        self.assertIn("Hi", out)
        self.assertIn("there & you", out)
        self.assertNotIn("var a", out)
        self.assertTrue(run("WebFetch", {"url": "file:///etc/passwd"}, self.cwd).startswith("Error:"))

    def test_glob_accepts_an_absolute_pattern(self):
        out = run("Glob", {"pattern": os.path.join(self.cwd, "src", "*.py")}, self.cwd)
        self.assertEqual(out.splitlines(), ["src/app.py"])

    def test_no_tool_error_ever_escapes(self):
        from unittest import mock
        def boom(*a, **k):
            raise NotImplementedError("boom")
        with mock.patch.dict(tools.SYNC, {"Glob": boom}):
            self.assertTrue(run("Glob", {"pattern": "*"}, self.cwd).startswith("Error:"))

    def test_grep_on_a_missing_path_is_an_error(self):
        self.assertTrue(run("Grep", {"pattern": "x", "path": "nope"}, self.cwd).startswith("Error:"))

    def test_unknown_tool_and_bad_arguments(self):
        self.assertTrue(run("Teleport", {}, self.cwd).startswith("Error:"))
        self.assertTrue(run("Read", {}, self.cwd).startswith("Error:"))
        self.assertTrue(run("Edit", {"file_path": "notes.md"}, self.cwd).startswith("Error:"))

    def test_results_are_clipped(self):
        (Path(self.cwd) / "big.txt").write_text("x" * 100 + "\n" * 1 + ("y" * 200 + "\n") * 1500, encoding="utf-8")
        self.assertLessEqual(len(run("Read", {"file_path": "big.txt"}, self.cwd)), tools.MAX_RESULT + 100)

    def test_offered_tools_follow_the_role(self):
        names = lambda specs: [s["function"]["name"] for s in specs]   # noqa: E731
        self.assertEqual(names(tools.offered({"writes": []}, has_bash=False)), ["Read", "Glob", "Grep", "WebFetch"])
        self.assertEqual(names(tools.offered({"writes": ["**"]}, has_bash=True)),
                         ["Read", "Glob", "Grep", "WebFetch", "Write", "Edit", "Bash"])
        for spec in tools.offered({"writes": ["**"]}, has_bash=True):
            self.assertEqual(spec["type"], "function")
            self.assertEqual(spec["function"]["parameters"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
