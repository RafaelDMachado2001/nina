import unittest

from nina import winproc


class FlagsTest(unittest.TestCase):
    def test_no_window_unless_a_console_was_asked_for(self):
        self.assertEqual(winproc.flags_for(0), winproc.CREATE_NO_WINDOW)
        self.assertEqual(winproc.flags_for(0x200), 0x200 | winproc.CREATE_NO_WINDOW)     # CREATE_NEW_PROCESS_GROUP
        self.assertEqual(winproc.flags_for(winproc.CREATE_NEW_CONSOLE), winproc.CREATE_NEW_CONSOLE)
        self.assertEqual(winproc.flags_for(winproc.DETACHED_PROCESS), winproc.DETACHED_PROCESS)

    def test_patch_is_applied_once_and_passes_the_flags(self):
        seen = []

        class FakePopen:
            def __init__(self, *args, creationflags=0, **kwargs):
                seen.append((args, creationflags, kwargs))

        self.assertTrue(winproc.hide_child_consoles(FakePopen))
        self.assertFalse(winproc.hide_child_consoles(FakePopen))
        FakePopen(["claude"], stdin=-1)
        FakePopen(["powershell"], creationflags=winproc.CREATE_NEW_CONSOLE)
        self.assertEqual(seen, [((["claude"],), winproc.CREATE_NO_WINDOW, {"stdin": -1}),
                                ((["powershell"],), winproc.CREATE_NEW_CONSOLE, {})])


if __name__ == "__main__":
    unittest.main()
