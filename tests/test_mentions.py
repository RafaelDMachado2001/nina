import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nina.mentions import Suggestion, active_mention, expand_home, format_mention, suggest

class ActiveMentionTest(unittest.TestCase):
    def test_finds_the_mention_under_the_cursor(self):
        self.assertEqual(active_mention("@src/ma", 7), (0, "src/ma"))
        self.assertEqual(active_mention("read @docs/a.md", 15), (5, "docs/a.md"))
        self.assertEqual(active_mention("read @", 6), (5, ""))
        self.assertEqual(active_mention('look at @"my fi', 15), (8, "my fi"))

    def test_not_a_mention(self):
        self.assertIsNone(active_mention("me@example.com", 14))
        self.assertIsNone(active_mention("hello world", 5))
        self.assertIsNone(active_mention("@a done ", 8))          # the cursor is past the mention

    def test_only_the_text_before_the_cursor_counts(self):
        self.assertEqual(active_mention("@abc def", 3), (0, "ab"))

class FormatTest(unittest.TestCase):
    def test_spaces_are_quoted(self):
        self.assertEqual(format_mention("a/b.md"), "@a/b.md")
        self.assertEqual(format_mention("my docs/a b.md"), '@"my docs/a b.md"')

    def test_home_is_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/x"}):
            self.assertEqual(expand_home("see @~/n.md and @~ and @\"~/a b\" me@~x"),
                             'see @/home/x/n.md and @/home/x and @"/home/x/a b" me@~x')

class SuggestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for f in ("README.md", "src/app/main.py", "src/app/util.py", "docs/guide.md", ".git/config", ".env",
                  "node_modules/x/index.js", "notes/Main Notes.txt"):
            (self.root / f).parent.mkdir(parents=True, exist_ok=True)
            (self.root / f).write_text("x")

    def values(self, query, **kw):
        return [s.value for s in suggest(query, str(self.root), **kw)]

    def test_empty_query_lists_the_top_level_folders_first(self):
        self.assertEqual(self.values(""), ["docs/", "notes/", "src/", "README.md"])

    def test_search_by_name_ignores_hidden_and_vendored_folders(self):
        self.assertEqual(self.values("main"), ["notes/Main Notes.txt", "src/app/main.py"])
        self.assertEqual(self.values("index"), [])
        self.assertEqual(self.values("config"), [])
        self.assertEqual(self.values(".env"), [])

    def test_name_matches_come_before_path_matches_and_fuzzy(self):
        self.assertEqual(self.values("app")[0], "src/app/")
        self.assertIn("src/app/main.py", self.values("app"))
        self.assertIn("src/app/util.py", self.values("sapu"))     # fuzzy: s-a-p-u in "src/app/util.py"

    def test_limit(self):
        self.assertEqual(len(self.values("", limit=2)), 2)

    def test_path_completion_one_level_at_a_time(self):
        self.assertEqual(self.values("./sr"), ["./src/"])
        self.assertEqual(self.values("./src/"), ["./src/app/"])
        self.assertEqual(self.values("./src/app/m"), ["./src/app/main.py"])
        self.assertEqual(self.values(f"{self.root}/do"), [f"{self.root}/docs/"])
        self.assertIn(f"../{self.root.name}/", self.values("../", limit=10000))
        self.assertEqual(self.values("./.e"), ["./.env"])         # hidden files only when you type the dot
        self.assertNotIn("./.env", self.values("./"))

    def test_home_completion(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(self.values("~"), ["~/"])
            self.assertEqual(self.values("~/do"), ["~/docs/"])

    def test_a_missing_folder_gives_nothing(self):
        self.assertEqual(self.values("/nonexistent-dir-xyz/a"), [])

    def test_directories_are_marked(self):
        self.assertEqual(suggest("./sr", str(self.root)), [Suggestion("./src/", True)])
        self.assertFalse(suggest("README", str(self.root))[0].is_dir)

if __name__ == "__main__":
    unittest.main()
