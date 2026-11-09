import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nina import config, shortcut


class FakeGsettings:
    """Keeps a dict of (schema:path, key) -> value, like dconf would."""
    def __init__(self, listing="@as []"):
        self.store = {(shortcut.SCHEMA, "custom-keybindings"): listing}
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        _g, op, *rest = cmd
        if op == "get":
            return subprocess.CompletedProcess(cmd, 0, self.store.get((rest[0], rest[1]), "''") + "\n", "")
        if op == "set":
            self.store[(rest[0], rest[1])] = rest[2]
        elif op == "reset":
            self.store.pop((rest[0], rest[1]), None)
        return subprocess.CompletedProcess(cmd, 0, "", "")


class ShortcutTest(unittest.TestCase):
    """GNOME."""
    def setUp(self):
        p = mock.patch.object(config, "WINDOWS", False)
        p.start()
        self.addCleanup(p.stop)

    def test_install_registers_the_binding_and_is_idempotent(self):
        g = FakeGsettings()
        self.assertEqual(shortcut.install("<Super>n", "nina chat", run=g), "<Super>n")
        shortcut.install("<Super>m", "nina chat", run=g)
        listing = shortcut._parse_list(g.store[(shortcut.SCHEMA, "custom-keybindings")])
        self.assertEqual(listing, [shortcut.PATH])
        item = f"{shortcut.ITEM_SCHEMA}:{shortcut.PATH}"
        self.assertEqual((g.store[(item, "binding")], g.store[(item, "command")]), ("<Super>m", "nina chat"))

    def test_other_custom_shortcuts_are_kept(self):
        other = shortcut.BASE + "custom0/"
        g = FakeGsettings(f"['{other}']")
        shortcut.install(run=g)
        self.assertEqual(shortcut._parse_list(g.store[(shortcut.SCHEMA, "custom-keybindings")]), [other, shortcut.PATH])
        self.assertTrue(shortcut.uninstall(run=g))
        self.assertEqual(shortcut._parse_list(g.store[(shortcut.SCHEMA, "custom-keybindings")]), [other])
        self.assertFalse(shortcut.uninstall(run=g))

    def test_default_command_runs_nina_open_with_this_python(self):
        self.assertTrue(shortcut.default_command().endswith("-m nina open"))

    def test_missing_gsettings_is_a_clear_error(self):
        def boom(cmd, **kw):
            raise FileNotFoundError("gsettings")

        with self.assertRaises(RuntimeError):
            shortcut.install(run=boom)


class WindowsShortcutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(config, "WINDOWS", True),
                  mock.patch.dict("os.environ", {"APPDATA": self.tmp.name}),
                  mock.patch.object(shortcut, "icon_file", return_value=Path("C:/n/nina.ico")),
                  mock.patch.object(config, "gui_python", return_value="C:/py/pythonw.exe")):
            p.start()
            self.addCleanup(p.stop)
        self.scripts = []

    def fake_ps(self, cmd, **kw):
        self.scripts.append(base64.b64decode(cmd[-1]).decode("utf-16-le"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_hotkey_format(self):
        f = shortcut.windows_hotkey
        self.assertEqual(f("Ctrl+Alt+N"), "CTRL+ALT+N")
        self.assertEqual(f("<Ctrl><Alt>n"), "CTRL+ALT+N")
        self.assertEqual(f("<Control><Shift>k"), "CTRL+SHIFT+K")
        self.assertEqual(f("alt+shift+F9"), "SHIFT+ALT+F9")
        for bad in ("<Super><Shift>n", "Shift+N", "N", ""):
            with self.assertRaises(RuntimeError, msg=bad):
                f(bad)

    def test_install_creates_a_start_menu_shortcut_with_the_hotkey(self):
        self.assertEqual(shortcut.install(shortcut.default_keys(), run=self.fake_ps), "CTRL+ALT+N")
        script = self.scripts[0]
        lnk = str(shortcut.programs_dir() / shortcut.LNK_NAME)
        self.assertTrue(lnk.startswith(self.tmp.name))
        for part in (f"CreateShortcut('{lnk}')", "$s.TargetPath = 'C:/py/pythonw.exe'", "$s.Arguments = '-m nina open'",
                     "$s.Hotkey = 'CTRL+ALT+N'", "$s.IconLocation = ", "$s.Save()"):
            self.assertIn(part, script)
        self.assertEqual(shortcut.default_command(), '"C:/py/pythonw.exe" -m nina open')

    def test_uninstall_removes_only_ninas_shortcut(self):
        self.assertFalse(shortcut.uninstall())
        lnk = shortcut.programs_dir() / shortcut.LNK_NAME
        lnk.parent.mkdir(parents=True)
        lnk.write_bytes(b"x")
        (lnk.parent / "Other.lnk").write_bytes(b"x")
        self.assertTrue(shortcut.uninstall())
        self.assertEqual([p.name for p in lnk.parent.iterdir()], ["Other.lnk"])

    def test_a_custom_command_is_refused(self):
        with self.assertRaises(RuntimeError):
            shortcut.install("Ctrl+Alt+N", "something else", run=self.fake_ps)


if __name__ == "__main__":
    unittest.main()
