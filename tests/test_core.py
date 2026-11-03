import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import IsolatedHome

from nina import claude_hooks, config, launcher
from nina.sockpath import compute_sock_path
from nina.widget import WORK_TIMEOUT_S, Nina


class FreshInstallTest(IsolatedHome):
    def test_neutral_defaults(self):
        cfg = config.load_config()
        self.assertEqual((cfg["account"], cfg["profiles"], cfg["language"]), ("", {}, "en"))
        self.assertEqual(config.list_accounts(), [])
        self.assertEqual(config.settings_path(), self.home / ".claude" / "settings.json")
        self.assertEqual(launcher.claude_command("", {}), "command claude")   # no account: plain ~/.claude

    def test_open_claude_with_no_configuration_at_all(self):
        launched = {}
        with mock.patch.object(launcher.shutil, "which", lambda t: t if t == "gnome-terminal" else None), \
                mock.patch.object(launcher.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd)), \
                mock.patch.object(launcher.os, "geteuid", lambda: 1000):
            cfg = config.load_config()
            err = launcher.open_claude(config.default_dir_of(cfg), "", cfg["account"], config.profile_of(cfg))
        self.assertIsNone(err)
        self.assertNotIn("CLAUDE_CONFIG_DIR", launched["cmd"][-1])

    def test_a_configured_account_that_does_not_exist_warns(self):
        self.assertIn("missing", launcher.open_claude(str(self.home), "", "missing", {}))

    def test_account_and_profile_flags(self):
        (self.accounts / "work").mkdir(parents=True)
        ctx = self.home / "ctx.md"
        ctx.write_text("Be brief.")
        cmd = launcher.claude_command("work", {"add_dirs": ["/srv/a b"], "context_file": str(ctx)})
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.accounts / 'work'}", cmd)
        self.assertIn("unset ANTHROPIC_API_KEY", cmd)
        self.assertIn("--add-dir '/srv/a b'", cmd)
        self.assertIn("--append-system-prompt 'Be brief.'", cmd)

    def test_socket_path(self):
        self.assertEqual(compute_sock_path(str(self.home), 7), str(self.home / "nina.sock"))
        self.assertEqual(compute_sock_path("", 7), "/tmp/nina-7.sock")
        self.assertEqual(compute_sock_path("/does/not/exist", 7), "/tmp/nina-7.sock")
        deep = self.home / ("d" * 60) / ("e" * 60)   # too long for a Unix socket path
        deep.mkdir(parents=True)
        self.assertEqual(compute_sock_path(str(deep), 7), "/tmp/nina-7.sock")

    def test_corrupt_config_falls_back_to_defaults(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("{this is not json")
        self.assertEqual(config.load_config()["account"], "")


class HooksInstallerTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.root_settings = self.home / ".claude" / "settings.json"
        self.root_settings.parent.mkdir(parents=True)
        self.root_settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]}}))
        (self.accounts / "personal").mkdir(parents=True)
        os.symlink(self.root_settings, self.accounts / "personal" / "settings.json")   # like a multi-account manager
        self.write_config({"account": "personal"})

    def hooks(self):
        return json.loads(self.root_settings.read_text())["hooks"]

    def test_installs_without_deleting_anything_and_is_idempotent(self):
        self.assertFalse(claude_hooks.hooks_installed())
        claude_hooks.install_hooks()
        self.assertTrue(claude_hooks.hooks_installed())
        self.assertEqual(set(claude_hooks.HOOK_EVENTS) - set(self.hooks()), set())
        self.assertTrue((self.accounts / "personal" / "settings.json").is_symlink())   # the link stays a link
        stop_cmds = [h["command"] for g in self.hooks()["Stop"] for h in g["hooks"]]
        self.assertIn("echo other", stop_cmds)                                           # the existing hook is preserved
        self.assertEqual(json.loads(self.root_settings.read_text())["model"], "x")
        self.assertIn("already installed", claude_hooks.install_hooks())
        self.assertEqual(sum("nina.hook" in c for c in stop_cmds), 1)
        self.assertTrue(all(c.endswith("-m nina.hook") for c in stop_cmds if "nina" in c))
        self.assertEqual(self.hooks()["PreToolUse"][0]["matcher"], "*")
        self.assertTrue(list(self.root_settings.parent.glob("settings.json.nina-bak-*")))

    def test_legacy_script_entries_are_migrated(self):
        data = json.loads(self.root_settings.read_text())
        data["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "/old/venv/bin/python /old/nina_hook.py", "timeout": 2})
        self.root_settings.write_text(json.dumps(data))
        claude_hooks.install_hooks()
        cmds = [h["command"] for g in self.hooks()["Stop"] for h in g["hooks"]]
        self.assertFalse(any("nina_hook.py" in c for c in cmds))
        self.assertEqual(sum(c.endswith("-m nina.hook") for c in cmds), 1)

    def test_uninstall_only_removes_ninas_entries(self):
        claude_hooks.install_hooks()
        claude_hooks.uninstall_hooks()
        self.assertFalse(claude_hooks.hooks_installed())
        data = json.loads(self.root_settings.read_text())
        self.assertEqual(list(data["hooks"]), ["Stop"])
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "echo other")

    def test_invalid_settings_are_not_overwritten(self):
        self.root_settings.write_text("{broken")
        with self.assertRaises(ValueError):
            claude_hooks.install_hooks()
        self.assertEqual(self.root_settings.read_text(), "{broken")


class HookScriptTest(unittest.TestCase):
    def run_hook(self, payload, runtime):
        return subprocess.run([sys.executable, "-m", "nina.hook"], input=payload, capture_output=True, text=True, timeout=10,
                              env={"PATH": os.environ.get("PATH", ""), "XDG_RUNTIME_DIR": str(runtime)})

    def test_sends_one_line_and_prints_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            got = []
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(os.path.join(d, "nina.sock"))
            srv.listen(1)

            def accept():
                conn, _addr = srv.accept()
                got.append(conn.recv(65536).decode())
                conn.close()

            t = threading.Thread(target=accept)
            t.start()
            r = self.run_hook(json.dumps({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash",
                                          "tool_input": {"command": "ls   -la\n/x" + "a" * 500}, "cwd": "/p"}), d)
            t.join(5)
            srv.close()
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        line = got[0]
        self.assertTrue(line.startswith("hook ") and line.endswith("\n"))
        data = json.loads(line[5:])
        self.assertEqual((data["ev"], data["sid"], data["tool"], data["cwd"]), ("PreToolUse", "s", "Bash", "/p"))
        self.assertLessEqual(len(data["detail"]), 160)
        self.assertNotIn("\n", data["detail"])

    def test_never_gets_in_claudes_way(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for payload in ('{"hook_event_name":"Stop","session_id":"s"}', "not json", "", "[]", '{"hook_event_name":'):
                t0 = time.time()
                r = self.run_hook(payload, d)                      # no NINA listening
                self.assertEqual((r.returncode, r.stdout), (0, ""), payload)
                self.assertLess(time.time() - t0, 2.0)


class MascotStateTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.n = Nina()
        self.addCleanup(lambda: (self.n.shutdown(), self.n.close()))

    def hook(self, **k):
        self.n.handle_hook(k)

    def test_priority_and_dismiss(self):
        self.assertEqual(self.n.mode(), "normal")
        self.hook(ev="UserPromptSubmit", sid="a")
        self.assertEqual(self.n.mode(), "work")
        self.hook(ev="PreToolUse", sid="a", tool="Edit")
        self.assertEqual(self.n.bubble_text, "Editing...")
        self.hook(ev="UserPromptSubmit", sid="b")
        self.hook(ev="Notification", sid="b", ntype="permission_prompt")
        self.assertEqual((self.n.mode(), self.n.current_wait_text()), ("wait", "I need your permission!"))
        self.hook(ev="Stop", sid="a")
        self.assertEqual(self.n.mode(), "wait")                    # waiting > working
        self.n.dismiss_waiting()
        self.assertEqual((self.n.mode(), self.n.bubble_text), ("normal", "Go to the terminal!"))

    def test_bubble_repeats_only_when_the_tool_changes(self):
        self.hook(ev="UserPromptSubmit", sid="a")
        self.hook(ev="PreToolUse", sid="a", tool="Edit")
        self.n.bubble_text = ""
        self.hook(ev="PreToolUse", sid="a", tool="Write")
        self.assertEqual(self.n.bubble_text, "")
        self.hook(ev="PreToolUse", sid="a", tool="Bash")
        self.assertEqual(self.n.bubble_text, "Running a command...")

    def test_granted_permission_goes_back_to_working(self):
        self.hook(ev="Notification", sid="c", ntype="idle_prompt")
        self.assertEqual(self.n.current_wait_text(), "I'm waiting for you!")
        self.hook(ev="PostToolUse", sid="c", tool="Bash")
        self.assertEqual(self.n.mode(), "work")

    def test_three_minute_safety_net_and_session_end(self):
        self.hook(ev="UserPromptSubmit", sid="c")
        self.n.sessions["c"]["last"] -= WORK_TIMEOUT_S + 5
        self.n.tick()
        self.assertEqual(self.n.mode(), "normal")
        self.hook(ev="UserPromptSubmit", sid="d")
        self.hook(ev="SessionEnd", sid="d")
        self.assertEqual(self.n.mode(), "normal")

    def test_stop_celebrates(self):
        self.hook(ev="Stop", sid="e")
        self.assertGreater(self.n.happy_ticks, 0)
        self.assertEqual(self.n.bubble_text, "Done!")

    def test_events_feed_the_agents_panel(self):
        self.hook(ev="UserPromptSubmit", sid="a", prompt="Task", cwd="/p")
        self.hook(ev="SubagentStart", sid="a", aid="x", atype="Explore")
        self.assertEqual(self.n.agents_active, 2)
        self.assertEqual(self.n.mode(), "work")                    # a subagent does not change the mascot by itself
        self.hook(ev="Stop", sid="a", last="ok")
        (agent,) = self.n.agents.list_agents()
        self.assertEqual(agent["display"], "done")

    def test_corrupt_agents_database_is_set_aside_and_recreated(self):
        state = Path(self.tmp.name) / "corrupt-state"
        (state / "nina").mkdir(parents=True)
        (state / "nina" / "agents.db").write_bytes(b"this is not a sqlite database" * 100)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            n = Nina()
            self.addCleanup(lambda: (n.shutdown(), n.close()))
            self.assertIsNotNone(n.agents)                          # the panel keeps working
            n.handle_hook({"ev": "UserPromptSubmit", "sid": "a", "prompt": "hi"})
            self.assertEqual(n.mode(), "work")
            self.assertEqual(len(n.agents.list_agents()), 1)
        kept = list((state / "nina").glob("agents.db.corrupt-*"))
        self.assertEqual(len(kept), 1)                              # the bad file was kept, not deleted
        self.assertTrue(kept[0].read_bytes().startswith(b"this is not"))


class PluginApiTest(IsolatedHome):
    def make_plugin(self, name, body):
        d = self.config_file.parent / "plugins"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)

    def test_plugin_setup_menu_and_alert(self):
        self.make_plugin("demo.py", (
            "calls = []\n"
            "def setup(nina):\n"
            "    calls.append('setup')\n"
            "    nina.set_alert('demo', '3 things need you', count=3, on_click=lambda: calls.append('clicked'))\n"
            "def menu(nina, menu):\n"
            "    menu.addAction('Demo action')\n"))
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        (mod,) = n.plugins
        self.assertEqual(mod.calls, ["setup"])
        self.assertEqual((n.mode(), n.current_wait_text(), n.get_alert("demo")["count"]), ("wait", "3 things need you", 3))
        from PySide6.QtWidgets import QMenu

        from nina import plugins
        menu = QMenu()
        plugins.build_menus(n.plugins, n, menu)
        self.assertEqual([a.text() for a in menu.actions()], ["Demo action"])
        n.dismiss_waiting()                                          # clicking dismisses and calls the handler
        self.assertEqual((n.mode(), mod.calls), ("normal", ["setup", "clicked"]))

    def test_claude_permission_wins_over_a_plugin_alert(self):
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        n.set_alert("demo", "plugin alert")
        n.handle_hook({"ev": "Notification", "sid": "c", "ntype": "permission_prompt"})
        self.assertEqual(n.current_wait_text(), "I need your permission!")
        n.dismiss_waiting()                                          # only Claude's request is dismissed first
        self.assertEqual(n.current_wait_text(), "plugin alert")

    def test_alerts_expire(self):
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        n.set_alert("demo", "x", ttl_s=10)
        n.sessions["alert:demo"]["last"] -= 11
        n.tick()
        self.assertIsNone(n.get_alert("demo"))

    def test_broken_plugins_never_take_nina_down(self):
        self.make_plugin("bad_import.py", "raise RuntimeError('boom at import')\n")
        self.make_plugin("bad_setup.py", "def setup(nina):\n    raise RuntimeError('boom in setup')\n")
        self.make_plugin("bad_menu.py", "def menu(nina, menu):\n    raise RuntimeError('boom in menu')\n")
        self.make_plugin("_ignored.py", "raise SystemExit('should never load')\n")
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        self.assertEqual([m.__name__ for m in n.plugins], ["nina_plugin_bad_menu"])
        from PySide6.QtWidgets import QMenu

        from nina import plugins
        plugins.build_menus(n.plugins, n, QMenu())                   # must not raise
        n.handle_hook({"ev": "UserPromptSubmit", "sid": "a"})
        self.assertEqual(n.mode(), "work")

    def test_plugins_can_be_disabled(self):
        self.make_plugin("demo.py", "def setup(nina):\n    nina.set_alert('x', 'y')\n")
        self.write_config({"plugins": False})
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        self.assertEqual((n.plugins, n.mode()), ([], "normal"))

    def test_run_process_ok_failure_and_timeout(self):
        from PySide6.QtCore import QEventLoop, QTimer
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))

        def run(program, args, timeout=30):
            loop, res = QEventLoop(), {}
            n.run_process(program, args, lambda c, o, e: (res.update(c=c, o=o, e=e), loop.quit()), timeout)
            QTimer.singleShot(15000, loop.quit)
            t0 = time.monotonic()
            loop.exec()
            res["dt"] = time.monotonic() - t0
            return res

        r = run("/bin/sh", ["-c", "echo hello"])
        self.assertEqual((r["c"], r["o"]), (0, "hello"))
        r = run("/bin/sh", ["-c", "echo bad >&2; exit 3"])
        self.assertEqual((r["c"], r["e"]), (3, "bad"))
        r = run("/bin/sh", ["-c", "sleep 8"], timeout=1)
        self.assertEqual(r["c"], 124)
        self.assertLess(r["dt"], 4)
        r = run("/nonexistent/program", [])
        self.assertEqual(r["c"], 1)


class ExportTest(IsolatedHome):
    def test_export_images(self):
        from nina.sprites import FRAMES, export_images
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        export_images(out)
        self.assertTrue((out / "nina.png").stat().st_size > 0 and (out / "nina_frames.png").stat().st_size > 0)
        self.assertTrue({"idle", "blink", "happy", "sleep", "work", "wait"} <= set(FRAMES))


if __name__ == "__main__":
    unittest.main()
