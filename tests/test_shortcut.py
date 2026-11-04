import subprocess
import unittest

from nina import shortcut


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

    def test_default_command_runs_nina_chat_with_this_python(self):
        self.assertTrue(shortcut.default_command().endswith("-m nina chat"))

    def test_missing_gsettings_is_a_clear_error(self):
        def boom(cmd, **kw):
            raise FileNotFoundError("gsettings")

        with self.assertRaises(RuntimeError):
            shortcut.install(run=boom)


if __name__ == "__main__":
    unittest.main()
