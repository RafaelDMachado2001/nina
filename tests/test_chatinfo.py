import unittest
from datetime import datetime
from pathlib import Path

from tests.support import IsolatedHome  # noqa: F401

from nina import chatinfo


class GitBranchTest(IsolatedHome):
    def test_reads_the_branch_from_a_subfolder_and_a_detached_head(self):
        repo = self.home / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "pkg").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/login\n", encoding="utf-8")
        self.assertEqual(chatinfo.git_branch(str(repo / "src" / "pkg")), "feature/login")
        (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
        self.assertEqual(chatinfo.git_branch(str(repo)), "0123456")

    def test_worktree_file_and_non_repository(self):
        real = self.home / "main" / ".git" / "worktrees" / "wt"
        real.mkdir(parents=True)
        (real / "HEAD").write_text("ref: refs/heads/side\n", encoding="utf-8")
        wt = self.home / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
        self.assertEqual(chatinfo.git_branch(str(wt)), "side")
        plain = self.home / "plain"
        plain.mkdir()
        self.assertIsNone(chatinfo.git_branch(str(plain)))      # tmp dirs have no repository above them


class FormatTest(unittest.TestCase):
    def test_clock_is_iso_date_and_twelve_hour_time(self):
        self.assertEqual(chatinfo.clock_text(datetime(2026, 11, 5, 15, 7)), ("2026-11-05", "3:07 PM"))
        self.assertEqual(chatinfo.clock_text(datetime(2026, 11, 5, 0, 5))[1], "12:05 AM")
        self.assertEqual(chatinfo.clock_text(datetime(2026, 11, 5, 12, 0))[1], "12:00 PM")

    def test_model_names_and_numbers(self):
        self.assertEqual(chatinfo.friendly_model("claude-sonnet-5-5"), "Sonnet 5.5")
        self.assertEqual(chatinfo.friendly_model("claude-opus-4-1-20250805"), "Opus 4.1")
        self.assertEqual(chatinfo.friendly_model("something-else"), "something-else")
        self.assertEqual([chatinfo.compact(n) for n in (950, 12300, 1_000_000, 250_000)], ["950", "12.3k", "1M", "250k"])
        self.assertEqual((chatinfo.money(0.0083), chatinfo.money(1.234)), ("$0.008", "$1.23"))


class UsageTest(IsolatedHome):
    NOW = datetime(2026, 11, 5, 15, 7)
    INFO = {"model": "claude-sonnet-5-5", "ctx_tokens": 26256, "ctx_max": 1_000_000, "cost": 0.0083, "tokens": 26300}
    SESSION = {"windows": {"five_hour": {"utilization": 0.14, "resetsAt": 1793894400},
                           "seven_day": {"utilization": 0.21, "resetsAt": 1794139200}}}

    def texts(self, level, rate):
        return [t for _k, t, _s in chatinfo.compose(level, "/home/u/nina", "main", self.INFO, rate, self.NOW)]

    def test_plans_with_a_session_show_percentages(self):
        self.assertEqual(self.texts(0, self.SESSION),
                         ["nina (main)", "Sonnet 5.5", "ctx 3%", "session 14%", "2026-11-05 3:07 PM"])

    def test_accounts_without_a_session_show_real_consumption(self):
        self.assertEqual(self.texts(0, None)[3], "$0.008 · 26.3k tok")
        rows = dict(chatinfo.details("~/nina", "main", "work", self.INFO, None, {"cost": 1.5, "tokens": 9000}, self.NOW))
        self.assertIn("pay as you go", rows["Usage"])
        self.assertIn("$1.50", rows["Today (NINA)"])

    def test_narrow_windows_drop_the_least_important_items_first(self):
        self.assertEqual(self.texts(1, self.SESSION)[-1], "session 14%")             # clock gone
        self.assertEqual(self.texts(2, self.SESSION)[0], "nina")                     # branch gone
        self.assertEqual(self.texts(5, self.SESSION), ["ctx 3%"])                    # only context left

    def test_severity_follows_how_full_it_is(self):
        self.assertEqual([chatinfo.severity(f) for f in (0.2, 0.8, 0.95)], ["ok", "warn", "err"])

    def test_day_usage_accumulates_and_resets_on_a_new_day(self):
        path = self.home / "usage.json"
        chatinfo.add_day(0.5, 100, path)
        self.assertEqual(chatinfo.add_day(0.25, 50, path)["tokens"], 150)
        self.assertEqual(chatinfo.load_day(path, "2099-01-01"), {"date": "2099-01-01", "cost": 0.0, "tokens": 0})
