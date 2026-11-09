import json
import os
import tempfile
import unittest
from pathlib import Path

from nina.autoapprove import AutoMode, Rules, Step, analyze


def ops(command):
    return [(s.prefix, s.op, s.hard) for s in analyze(command)]


class ReadOnlyCommandsTest(unittest.TestCase):
    def test_plain_reads_need_nothing(self):
        for command in ("ls -la /var/log", "cat a.txt | grep foo | wc -l", "git status", "git log --oneline -5",
                        "git diff HEAD~1", "find . -name '*.py'", "pwd && ls", "echo hi 2>&1", "ls 2>/dev/null",
                        "sed -n '1,5p' file", "git branch", "git branch -a", "git tag -l", "docker ps -a",
                        "kubectl get pods -n x", "systemctl status nginx", "curl -s https://example.com/health",
                        "echo ${HOME}", "ls $(pwd)", "cd /tmp && ls", "awk '{print $1}' file"):
            self.assertEqual(ops(command), [], command)

    def test_reads_over_ssh(self):
        self.assertEqual(ops('ssh servidor "ls /var/log | tail -5"'), [])
        self.assertEqual(ops("ssh -p 2222 -i key servidor cat /etc/hostname"), [])


class SensitiveCommandsTest(unittest.TestCase):
    def test_writes_ask(self):
        self.assertEqual(ops("rm -rf build"), [("rm", "run", False)])
        self.assertEqual(ops("git push origin main"), [("git push", "run", False)])
        self.assertEqual(ops("git branch -D old"), [("git branch", "run", False)])
        self.assertEqual(ops("find . -name '*.tmp' -delete"), [("find", "run", False)])
        self.assertEqual(ops("sed -i 's/a/b/' f"), [("sed", "run", False)])
        self.assertEqual(ops("curl -X POST https://x/api"), [("curl", "run", False)])
        self.assertEqual(ops("curl -d a=1 https://x/api"), [("curl", "run", False)])

    def test_a_pipeline_is_as_sensitive_as_its_worst_part(self):
        self.assertEqual(ops("ls && rm x"), [("rm", "run", False)])
        self.assertEqual(ops("echo $(rm x)"), [("rm", "run", False)])
        self.assertEqual(ops("echo `rm x`"), [("rm", "run", False)])

    def test_unknown_programs_ask(self):
        self.assertEqual(ops("mytool --go"), [("mytool", "run", False)])

    def test_never_remembered(self):
        for command in ("echo a > file", "ls >> log", "sudo ls", "echo 'oops", "cat <<EOF\nx\nEOF"):
            steps = analyze(command)
            self.assertTrue(any(s.hard for s in steps), command)

    def test_dev_null_is_not_a_write(self):
        self.assertEqual(ops("ls > /dev/null"), [])


class SqlTest(unittest.TestCase):
    def test_select_over_ssh_is_a_read(self):
        self.assertEqual(ops('ssh servidor "psql -c \'select * from t\'"'), [])
        self.assertEqual(ops("ssh servidor psql -c \"select count from t\""), [])
        self.assertEqual(ops("psql -c 'select 1'"), [])
        self.assertEqual(ops("psql -c '\\dt'"), [])

    def test_update_over_ssh_asks_with_the_ssh_prefix(self):
        self.assertEqual(ops('ssh servidor "psql -c \'update t set a=1 where id=2\'"'), [("ssh servidor", "UPDATE", False)])
        self.assertEqual(ops("ssh servidor psql -c \"delete from t where id=2\""), [("ssh servidor", "DELETE", False)])

    def test_update_or_delete_without_where_is_never_remembered(self):
        self.assertEqual(ops('ssh servidor "psql -c \'update t set a=1\'"'), [("ssh servidor", "UPDATE", True)])
        self.assertEqual(ops("psql -c 'delete from t'"), [("psql", "DELETE", True)])

    def test_sql_piped_in_is_still_seen(self):
        steps = analyze("echo 'delete from t where a=1' | psql mydb")
        self.assertEqual([(s.prefix, s.op) for s in steps], [("psql", "DELETE")])

    def test_sql_we_cannot_see_asks(self):
        self.assertEqual(ops("psql -f script.sql"), [("psql", "run", False)])
        self.assertEqual(ops("ssh servidor psql"), [("ssh servidor", "psql", False)])

    def test_each_operation_is_its_own_rule(self):
        self.assertEqual(ops("psql -c \"update a set x=1 where y=2; drop table b\""),
                         [("psql", "UPDATE", False), ("psql", "DROP", False)])


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auto-rules.json"

    def test_a_saved_rule_covers_the_same_prefix_and_operation_only(self):
        rules = Rules(self.path)
        rules.add([Step("ssh servidor", "UPDATE")])
        self.assertTrue(rules.covers(Step("ssh servidor", "UPDATE")))
        self.assertFalse(rules.covers(Step("ssh servidor", "DELETE")))
        self.assertFalse(rules.covers(Step("ssh outro", "UPDATE")))

    def test_hard_steps_are_never_saved_or_covered(self):
        rules = Rules(self.path)
        rules.add([Step("psql", "UPDATE", True)])
        self.assertEqual(rules.all(), [])
        rules.items.append(("psql", "UPDATE"))
        self.assertFalse(rules.covers(Step("psql", "UPDATE", True)))

    def test_rules_survive_a_restart_and_can_be_removed(self):
        Rules(self.path).add([Step("ssh servidor", "UPDATE"), Step("rm", "run")])
        again = Rules(self.path)
        self.assertEqual(again.all(), [("ssh servidor", "UPDATE"), ("rm", "run")])
        again.remove("rm", "run")
        self.assertEqual(Rules(self.path).all(), [("ssh servidor", "UPDATE")])
        if os.name == "posix":   # on Windows the user profile's ACL keeps it private
            self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_a_broken_file_means_no_rules(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(Rules(self.path).all(), [])
        self.path.write_text(json.dumps([{"prefix": 1}, "x", {"prefix": "a", "op": "b"}]), encoding="utf-8")
        self.assertEqual(Rules(self.path).all(), [("a", "b")])


class AutoModeTest(unittest.TestCase):
    def test_pending_is_what_still_needs_an_answer(self):
        with tempfile.TemporaryDirectory() as d:
            auto = AutoMode(True, Rules(Path(d) / "r.json"))
            update = 'ssh servidor "psql -c \'update t set a=1 where id=2\'"'
            self.assertEqual(auto.pending("ls"), [])
            self.assertEqual([s.label for s in auto.pending(update)], ["ssh servidor · UPDATE"])
            auto.rules.add(auto.pending(update))
            self.assertEqual(auto.pending(update), [])
            self.assertEqual(auto.pending('ssh servidor "psql -c \'update t set a=9 where id=1\'"'), [])
            self.assertEqual([s.label for s in auto.pending('ssh servidor "psql -c \'delete from t where id=2\'"')],
                             ["ssh servidor · DELETE"])
            self.assertTrue(auto.pending('ssh servidor "psql -c \'update t set a=1\'"')[0].hard)

    def test_the_switch_reports_changes(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            auto = AutoMode(False, Rules(Path(d) / "r.json"), seen.append)
            auto.set_enabled(True)
            self.assertEqual((auto.enabled(), seen), (True, [True]))


if __name__ == "__main__":
    unittest.main()
