import os

from tests.support import IsolatedHome  # noqa: F401

from nina import chathistory


def record(conv="abc1", **kw):
    base = {"id": conv, "title": "Fix login", "account": "work", "cwd": "/home/u/app", "session": "sess-1",
            "created": 100.0, "updated": 200.0, "cost": 0.5, "tokens": 900,
            "entries": [["user", "why does login fail?"], ["assistant", "The token expires."], ["tool", "Read: auth.py"]]}
    return {**base, **kw}


class HistoryStoreTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.base = self.home / "history"

    def test_save_load_round_trip_and_private_files(self):
        chathistory.save(record(), self.base)
        loaded = chathistory.load("abc1", self.base)
        self.assertEqual((loaded["title"], loaded["account"], loaded["session"], len(loaded["entries"])),
                         ("Fix login", "work", "sess-1", 3))
        for name in ("abc1.meta.json", "abc1.msgs.gz"):
            if os.name == "posix":   # on Windows the user profile's ACL keeps it private
                self.assertEqual(oct((self.base / name).stat().st_mode & 0o777), "0o600")
        self.assertEqual(chathistory.message_count(loaded), 2)       # tool lines are not messages

    def test_the_list_reads_only_the_small_meta_files(self):
        chathistory.save(record(), self.base)
        (self.base / "abc1.msgs.gz").unlink()                        # the list must not need the messages
        meta = chathistory.list_meta(self.base)[0]
        self.assertEqual((meta["title"], meta["count"], meta["preview"]), ("Fix login", 2, "why does login fail?"))
        self.assertNotIn("entries", meta)
        self.assertEqual(chathistory.load("abc1", self.base)["entries"], [])   # damaged body: empty, not a crash

    def test_messages_are_compressed_on_disk(self):
        big = [["user", "please explain this function in detail " * 20], ["assistant", "Sure, here is how it works. " * 200]] * 100
        chathistory.save(record(entries=big), self.base)
        raw = len(__import__("json").dumps(big).encode())
        packed = (self.base / "abc1.msgs.gz").stat().st_size
        self.assertLess(packed * 10, raw)
        count, size = chathistory.usage(self.base)
        self.assertEqual(count, 1)
        self.assertLess(size, raw // 5)
        self.assertEqual(len(chathistory.load("abc1", self.base)["entries"]), 200)

    def test_lists_newest_first_skips_empty_and_damaged(self):
        chathistory.save(record("a1", updated=10), self.base)
        chathistory.save(record("b2", updated=30), self.base)
        chathistory.save(record("c3", entries=[]), self.base)
        (self.base / "bad.meta.json").write_text("{not json", encoding="utf-8")
        (self.base / "..evil.meta.json").write_text("{}", encoding="utf-8")
        self.assertEqual([m["id"] for m in chathistory.list_meta(self.base)], ["b2", "a1"])

    def test_ids_cannot_escape_the_folder(self):
        chathistory.save(record("../../etc/x"), self.base)
        self.assertEqual(list(self.base.glob("**/*")) if self.base.exists() else [], [])
        self.assertIsNone(chathistory.load("../x", self.base))
        chathistory.delete("../x", self.base)

    def test_search_looks_in_title_account_folder_and_preview_only(self):
        chathistory.save(record(), self.base)
        m = chathistory.list_meta(self.base)[0]
        for q in ("login", "WORK", "app", "why does", "login work", ""):
            self.assertTrue(chathistory.matches(m, q), q)
        self.assertFalse(chathistory.matches(m, "token expires"))     # message text is not indexed
        self.assertFalse(chathistory.matches(m, "login banana"))

    def test_delete_removes_both_files_and_ids_are_unique(self):
        chathistory.save(record(), self.base)
        chathistory.delete("abc1", self.base)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertIsNone(chathistory.load("abc1", self.base))
        self.assertNotEqual(chathistory.new_id(), chathistory.new_id())
        self.assertRegex(chathistory.new_id(), r"^[0-9a-f]{1,16}$")

    def test_prune_forgets_old_conversations_except_the_ones_in_use(self):
        now = 100 * 86400
        chathistory.save(record("old1", updated=now - 40 * 86400), self.base)
        chathistory.save(record("old2", updated=now - 40 * 86400), self.base)
        chathistory.save(record("new1", updated=now - 2 * 86400), self.base)
        self.assertEqual(chathistory.prune(0, base=self.base, now=now), 0)           # off by default
        self.assertEqual(chathistory.prune(30, keep={"old2"}, base=self.base, now=now), 1)
        self.assertEqual(sorted(m["id"] for m in chathistory.list_meta(self.base)), ["new1", "old2"])
