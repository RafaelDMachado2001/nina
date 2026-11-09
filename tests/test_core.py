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
        self.assertEqual(config.settings_path(), Path(os.path.realpath(self.home / ".claude" / "settings.json")))
        self.assertEqual(launcher.claude_command("", {}), "command claude")   # no account: plain ~/.claude

    def test_open_claude_with_no_configuration_at_all(self):
        launched = {}
        with mock.patch.object(launcher.shutil, "which", lambda t: t if t == "gnome-terminal" else None), \
                mock.patch.object(launcher.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd)), \
                mock.patch.object(launcher.os, "geteuid", lambda: 1000, create=True):
            cfg = config.load_config()
            err = launcher.open_claude(config.default_dir_of(cfg), "", cfg["account"], config.profile_of(cfg),
                                       windows=False)
        self.assertIsNone(err)
        self.assertNotIn("CLAUDE_CONFIG_DIR", launched["cmd"][-1])

    def test_windows_opens_windows_terminal_with_an_encoded_powershell_script(self):
        import base64
        launched = {}
        (self.accounts / "work").mkdir(parents=True)
        with mock.patch.object(launcher.shutil, "which", lambda t: t if t == "wt" else None),                 mock.patch.object(launcher.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd, kw=kw)):
            err = launcher.open_claude(str(self.home), "", "work", {"add_dirs": ["D:/it's here"]}, resume="s-1",
                                       windows=True)
        self.assertIsNone(err)
        cmd = launched["cmd"]
        self.assertEqual(cmd[:4], ["wt", "-w", "new", "new-tab"])
        self.assertEqual(cmd[cmd.index("-d") + 1], str(self.home))
        self.assertEqual(cmd[-5:-1], ["powershell", "-NoLogo", "-NoExit", "-EncodedCommand"])   # no pwsh here
        script = base64.b64decode(cmd[-1]).decode("utf-16-le")
        self.assertIn(f"$env:CLAUDE_CONFIG_DIR = '{self.accounts / 'work'}'", script)
        self.assertIn("Remove-Item Env:ANTHROPIC_API_KEY", script)
        self.assertIn("'--resume' 's-1' '--add-dir'", script)
        self.assertIn("'D:", script)
        self.assertIn("it''s here'", script)                            # quotes are doubled, nothing is expanded
        self.assertIn("-CommandType Application", script)               # never a `claude` function or alias

    def test_windows_falls_back_to_a_powershell_console(self):
        launched = {}
        with mock.patch.object(launcher.shutil, "which", lambda t: t if t == "powershell" else None),                 mock.patch.object(launcher.subprocess, "Popen", lambda cmd, **kw: launched.update(cmd=cmd, kw=kw)):
            self.assertIsNone(launcher.open_claude(str(self.home), "", "", {}, windows=True))
        self.assertEqual(launched["cmd"][0], "powershell")
        self.assertNotIn("CLAUDE_CONFIG_DIR", __import__("base64").b64decode(launched["cmd"][-1]).decode("utf-16-le"))
        with mock.patch.object(launcher.shutil, "which", lambda t: None):
            self.assertIn("terminal", launcher.open_claude(str(self.home), "", "", {}, windows=True))

    def test_windows_autostart_uses_the_run_key_with_pythonw(self):
        store = {}

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def query(key, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name], 1

        def delete(key, name):
            if name not in store:
                raise FileNotFoundError(name)
            del store[name]

        fake = mock.Mock(HKEY_CURRENT_USER=1, REG_SZ=1, OpenKey=lambda *a: Key(), CreateKey=lambda *a: Key(),
                         QueryValueEx=query, DeleteValue=delete,
                         SetValueEx=lambda key, name, _r, _t, value: store.__setitem__(name, value))
        with mock.patch.dict(sys.modules, {"winreg": fake}),                 mock.patch.object(config, "gui_python", return_value="C:/py/pythonw.exe"):
            self.assertFalse(launcher.autostart_enabled(windows=True))
            launcher.set_autostart(True, windows=True)
            self.assertEqual(store, {"NINA": '"C:/py/pythonw.exe" -m nina'})
            self.assertTrue(launcher.autostart_enabled(windows=True))
            launcher.set_autostart(False, windows=True)
            launcher.set_autostart(False, windows=True)                  # twice is fine
            self.assertFalse(launcher.autostart_enabled(windows=True))

    def test_gui_python_prefers_pythonw(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "python.exe").write_bytes(b"")
            self.assertEqual(config.gui_python(str(Path(d) / "python.exe")), str(Path(d) / "python.exe"))
            (Path(d) / "pythonw.exe").write_bytes(b"")
            self.assertEqual(config.gui_python(str(Path(d) / "python.exe")), str(Path(d) / "pythonw.exe"))
            self.assertEqual(config.gui_python("/usr/bin/python3"), "/usr/bin/python3")

    def test_only_one_nina_holds_the_instance_lock(self):
        from nina import app as nina_app
        first = nina_app.instance_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(nina_app.instance_lock())          # a second NINA starting at the same moment
        first.unlock()
        again = nina_app.instance_lock()
        self.assertIsNotNone(again)
        again.unlock()

    def test_ps_quote(self):
        self.assertEqual(launcher.ps_quote("a'b $x"), "'a''b $x'")

    def test_a_configured_account_that_does_not_exist_warns(self):
        self.assertIn("missing", launcher.open_claude(str(self.home), "", "missing", {}))

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_account_and_profile_flags(self):
        (self.accounts / "work").mkdir(parents=True)
        ctx = self.home / "ctx.md"
        ctx.write_text("Be brief.", encoding="utf-8")
        cmd = launcher.claude_command("work", {"add_dirs": ["/srv/a b"], "context_file": str(ctx)})
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.accounts / 'work'}", cmd)
        self.assertIn("unset ANTHROPIC_API_KEY", cmd)
        self.assertIn("--add-dir '/srv/a b'", cmd)
        self.assertIn("--append-system-prompt 'Be brief.'", cmd)

    def test_resume_a_session(self):
        self.assertEqual(launcher.claude_command("", {}, resume="abc-123"), "command claude --resume abc-123")
        self.assertEqual(launcher.claude_command("", {}, resume="a b"), "command claude --resume 'a b'")

    def test_account_of_a_transcript(self):
        f = launcher.account_of_transcript
        self.assertEqual(f("/root/.claude/accounts/work/projects/-srv-x/abc.jsonl"), "work")
        self.assertEqual(f("/home/me/.claude/projects/-srv-x/abc.jsonl"), "")
        self.assertEqual(f(r"C:\Users\me\.claude\accounts\work\projects\C--x\abc.jsonl"), "work")
        self.assertEqual(f(None), "")

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_socket_path(self):
        self.assertEqual(compute_sock_path(str(self.home), 7), str(self.home / "nina.sock"))
        self.assertEqual(compute_sock_path("", 7), "/tmp/nina-7.sock")
        self.assertEqual(compute_sock_path("/does/not/exist", 7), "/tmp/nina-7.sock")
        deep = self.home / ("d" * 60) / ("e" * 60)   # too long for a Unix socket path
        deep.mkdir(parents=True)
        self.assertEqual(compute_sock_path(str(deep), 7), "/tmp/nina-7.sock")

    def test_corrupt_config_falls_back_to_defaults(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("{this is not json", encoding="utf-8")
        self.assertEqual(config.load_config()["account"], "")


class HooksInstallerTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.root_settings = self.home / ".claude" / "settings.json"
        self.root_settings.parent.mkdir(parents=True)
        self.root_settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]}}), encoding="utf-8")
        (self.accounts / "personal").mkdir(parents=True)
        try:
            os.symlink(self.root_settings, self.accounts / "personal" / "settings.json")   # like a multi-account manager
            self.linked = True
        except OSError:              # Windows without developer mode: no symlinks, the account has its own file
            self.linked = False
            self.root_settings = self.accounts / "personal" / "settings.json"
            self.root_settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo other"}]}]}}), encoding="utf-8")
        self.write_config({"account": "personal"})

    def hooks(self):
        return json.loads(self.root_settings.read_text(encoding="utf-8"))["hooks"]

    def test_installs_without_deleting_anything_and_is_idempotent(self):
        self.assertFalse(claude_hooks.hooks_installed())
        claude_hooks.install_hooks()
        self.assertTrue(claude_hooks.hooks_installed())
        self.assertEqual(set(claude_hooks.HOOK_EVENTS) - set(self.hooks()), set())
        self.assertEqual((self.accounts / "personal" / "settings.json").is_symlink(), self.linked)   # a link stays a link
        stop_cmds = [h["command"] for g in self.hooks()["Stop"] for h in g["hooks"]]
        self.assertIn("echo other", stop_cmds)                                           # the existing hook is preserved
        self.assertEqual(json.loads(self.root_settings.read_text(encoding="utf-8"))["model"], "x")
        self.assertIn("already installed", claude_hooks.install_hooks())
        self.assertEqual(sum("nina.hook" in c for c in stop_cmds), 1)
        self.assertTrue(all(c.endswith("-m nina.hook") for c in stop_cmds if "nina" in c))
        self.assertEqual(self.hooks()["PreToolUse"][0]["matcher"], "*")
        self.assertTrue(list(self.root_settings.parent.glob("settings.json.nina-bak-*")))

    def test_legacy_script_entries_are_migrated(self):
        data = json.loads(self.root_settings.read_text(encoding="utf-8"))
        data["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "/old/venv/bin/python /old/nina_hook.py", "timeout": 2})
        self.root_settings.write_text(json.dumps(data), encoding="utf-8")
        claude_hooks.install_hooks()
        cmds = [h["command"] for g in self.hooks()["Stop"] for h in g["hooks"]]
        self.assertFalse(any("nina_hook.py" in c for c in cmds))
        self.assertEqual(sum(c.endswith("-m nina.hook") for c in cmds), 1)

    def test_uninstall_only_removes_ninas_entries(self):
        claude_hooks.install_hooks()
        claude_hooks.uninstall_hooks()
        self.assertFalse(claude_hooks.hooks_installed())
        data = json.loads(self.root_settings.read_text(encoding="utf-8"))
        self.assertEqual(list(data["hooks"]), ["Stop"])
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "echo other")

    def test_invalid_settings_are_not_overwritten(self):
        self.root_settings.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            claude_hooks.install_hooks()
        self.assertEqual(self.root_settings.read_text(encoding="utf-8"), "{broken")


class HookScriptTest(unittest.TestCase):
    def run_hook(self, payload, runtime):
        return subprocess.run([sys.executable, "-m", "nina.hook"], input=payload, capture_output=True, text=True, timeout=10,
                              env={"PATH": os.environ.get("PATH", ""), "XDG_RUNTIME_DIR": str(runtime)})

    @unittest.skipIf(sys.platform == "win32", "Unix sockets; see test_windows_named_pipe_end_to_end")
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

    @unittest.skipUnless(sys.platform == "win32", "Windows named pipes")
    def test_windows_named_pipe_end_to_end(self):
        """The registered hook command (pythonw.exe -m nina.hook) reaches a real QLocalServer through the pipe."""
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalServer
        user = f"ninatest{os.getpid()}"
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.UserAccessOption)
        self.assertTrue(server.listen(f"nina-{user}"), server.errorString())
        self.addCleanup(server.close)
        got = bytearray()

        def on_connection():
            while server.hasPendingConnections():
                conn = server.nextPendingConnection()
                conn.readyRead.connect(lambda conn=conn: got.extend(bytes(conn.readAll())))

        server.newConnection.connect(on_connection)
        exe = claude_hooks.hook_command().split('"')[1]         # pythonw.exe when it exists
        env = {**os.environ, "USERNAME": user}
        for k in ("LOGNAME", "USER", "LNAME", "PYTHONIOENCODING", "PYTHONUTF8"):   # the plain locale, like Claude Code's
            env.pop(k, None)
        proc = subprocess.Popen([exe, "-m", "nina.hook"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        proc.stdin.write(json.dumps({"hook_event_name": "Stop", "session_id": "w", "cwd": "C:\\Users\\Mashí"},
                                    ensure_ascii=False).encode("utf-8"))      # UTF-8, not the console code page
        proc.stdin.close()
        deadline = time.monotonic() + 10
        while (proc.poll() is None or not got.endswith(b"\n")) and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        self.assertEqual((proc.wait(5), proc.stdout.read()), (0, b""))
        line = got.decode()
        self.assertTrue(line.startswith("hook ") and line.endswith("\n"), line)
        self.assertEqual(json.loads(line[5:])["cwd"], "C:\\Users\\Mashí")

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


class ChatIntegrationTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.n = Nina()
        self.launched = []
        self.n.launch = lambda *a, **k: self.launched.append(a)
        self.addCleanup(lambda: (self.n.shutdown(), self.n.close()))

    def release(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        p = QPointF(10, 10)

        def event(kind):
            return QMouseEvent(kind, p, p, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

        self.n.mousePressEvent(event(QEvent.MouseButtonPress))
        self.n.mouseReleaseEvent(event(QEvent.MouseButtonRelease))

    def double_click(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        p = QPointF(10, 10)
        self.n.mouseDoubleClickEvent(QMouseEvent(QEvent.MouseButtonDblClick, p, p, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

    def test_single_click_opens_nothing_when_the_chat_is_available(self):
        self.release()
        self.assertEqual(self.launched, [])
        self.assertIsNone(self.n.chat_box)

    def test_double_click_opens_the_chat_and_not_the_terminal(self):
        self.release()
        self.double_click()
        self.assertEqual(self.launched, [])
        self.assertTrue(self.n.chat_box.isVisible())
        self.double_click()
        self.assertFalse(self.n.chat_box.isVisible())   # toggles

    def test_chat_commands_and_mascot_mood(self):
        self.n.handle_command("chat")
        self.assertTrue(self.n.chat_box.isVisible())
        self.n.chat_controller.set_status("working")
        self.assertEqual(self.n.mode(), "work")
        self.n.chat_controller.set_status("idle")
        self.assertEqual(self.n.mode(), "normal")

    def test_ask_command_sends_the_text(self):
        sent = []
        self.n.ensure_chat()
        self.n.chat_controller.ask = lambda text: sent.append(text) or True
        self.n.handle_command("ask what changed today?")
        self.assertEqual(sent, ["what changed today?"])

    def test_menu_offers_the_chat(self):
        self.assertIn("Talk to NINA...", [a.text() for a in self.n.build_menu().actions()])


class PluginApiTest(IsolatedHome):
    def make_plugin(self, name, body):
        d = self.config_file.parent / "plugins"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body, encoding="utf-8")

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
            guard = QTimer()                 # owned here and stopped below: a bare singleShot would fire later, in
            guard.setSingleShot(True)        # another test, on this already deleted event loop (a crash on Linux)
            guard.timeout.connect(loop.quit)
            guard.start(15000)
            t0 = time.monotonic()
            loop.exec()
            guard.stop()
            res["dt"] = time.monotonic() - t0
            return res

        py = sys.executable
        r = run(py, ["-c", "print('hello')"])
        self.assertEqual((r["c"], r["o"]), (0, "hello"))
        r = run(py, ["-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((r["c"], r["e"]), (3, "bad"))
        r = run(py, ["-c", "import time; time.sleep(8)"], timeout=1)
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


class ConnectionsWiringTest(IsolatedHome):
    LOCAL = {"id": "local", "kind": "openai", "name": "Local AI", "base_url": "http://localhost:11434/v1", "model": "qwen"}

    def test_the_chat_is_available_with_only_a_custom_connection(self):
        from nina import widget
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        with mock.patch.object(widget.managed, "available", return_value=False):
            self.assertFalse(n.managed_available())
            n.cfg["connections"] = [self.LOCAL]
            self.assertTrue(n.managed_available())
            ctx = n.managed_context()
            self.assertEqual((ctx["sdk"], ctx["cfg"]["connections"][0]["id"]), (False, "local"))

    def test_agents_nina_runs_itself_reach_the_agents_panel(self):
        n = Nina()
        self.addCleanup(lambda: (n.shutdown(), n.close()))
        if n.agents is None:
            self.skipTest("agents store unavailable")
        ctrl = n.make_chat_controller("t1")
        ctrl.hook_event.emit({"ev": "UserPromptSubmit", "sid": "nina-s1", "prompt": "hello", "cwd": str(self.home)})
        self.assertIsNotNone(n.agents.get("nina-s1"))
