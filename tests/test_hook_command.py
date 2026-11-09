import tempfile
import unittest
from pathlib import Path

from nina import claude_hooks


class HookCommandTest(unittest.TestCase):
    def test_posix_quotes_the_interpreter(self):
        self.assertEqual(claude_hooks.hook_command(windows=False, executable="/opt/my py/bin/python"),
                         "'/opt/my py/bin/python' -m nina.hook")

    def test_windows_prefers_pythonw_next_to_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "python.exe").write_text("", encoding="utf-8")
            (Path(tmp) / "pythonw.exe").write_text("", encoding="utf-8")
            command = claude_hooks.hook_command(windows=True, executable=str(Path(tmp) / "python.exe"))
        self.assertEqual(command, f'"{Path(tmp).as_posix()}/pythonw.exe" -m nina.hook')

    def test_windows_keeps_python_when_pythonw_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "python.exe").write_text("", encoding="utf-8")
            command = claude_hooks.hook_command(windows=True, executable=str(Path(tmp) / "python.exe"))
        self.assertTrue(command.endswith('/python.exe" -m nina.hook'))

    def test_windows_backslashes_become_forward_slashes(self):
        command = claude_hooks.hook_command(windows=True, executable="C:\\Users\\Ana Lima\\venv\\Scripts\\nina.exe")
        self.assertEqual(command, '"C:/Users/Ana Lima/venv/Scripts/nina.exe" -m nina.hook')

    def test_every_form_is_recognised_as_a_nina_hook(self):
        for windows in (False, True):
            command = claude_hooks.hook_command(windows=windows, executable="/x/python")
            self.assertTrue(claude_hooks._is_nina_hook({"command": command}))


if __name__ == "__main__":
    unittest.main()
