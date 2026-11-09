import subprocess
import sys
import unittest
from unittest import mock

from nina import opener, sockpath


def done(code=0):
    return subprocess.CompletedProcess([], code, b"", b"")


class OpenerTest(unittest.TestCase):
    def test_a_running_nina_just_gets_the_command(self):
        sent = []
        self.assertEqual(opener.open_chat(send=lambda m: sent.append(m) or True), 0)
        self.assertEqual(sent, ["chat"])

    def test_a_closed_nina_is_started_with_the_password_prompt_then_asked_to_open(self):
        answers = iter([False, False, False, True])           # not up yet for a couple of polls
        ran, slept = [], []

        def run(cmd, **kw):
            ran.append(cmd)
            return done(0)                                    # `systemctl cat` finds the system service; start works

        code = opener.open_chat(send=lambda m: next(answers), run=run, sleep=slept.append, uid=1000, windows=False)
        self.assertEqual(code, 0)
        self.assertEqual(ran[0], ["systemctl", "cat", "nina.service"])
        self.assertEqual(ran[1], ["pkexec", "systemctl", "start", "nina.service"])
        self.assertEqual(len(slept), 2)

    def test_root_starts_the_service_without_pkexec_and_user_services_are_supported(self):
        def only_user_unit(cmd, **kw):
            return done(0 if "--user" in cmd else 1)

        self.assertEqual(opener.start_command(lambda c, **k: done(0), uid=0, windows=False), ["systemctl", "start", "nina.service"])
        self.assertEqual(opener.start_command(only_user_unit, uid=1000, windows=False), ["systemctl", "--user", "start", "nina.service"])

    def test_without_any_service_a_new_process_is_started(self):
        popen = mock.Mock()
        answers = iter([False, True])
        code = opener.open_chat(send=lambda m: next(answers), run=lambda c, **k: done(1), popen=popen,
                                sleep=lambda s: None, uid=1000, windows=False)
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args.args[0][1:], ["-m", "nina"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_a_failed_or_cancelled_start_is_reported(self):
        def run(cmd, **kw):
            return done(0 if cmd[1] == "cat" else 126)        # the password prompt was cancelled

        self.assertEqual(opener.open_chat(send=lambda m: False, run=run, uid=1000, windows=False), 1)

    def test_gives_up_when_she_never_answers(self):
        self.assertEqual(opener.open_chat(send=lambda m: False, run=lambda c, **k: done(0), sleep=lambda s: None,
                                          timeout=2, uid=0, windows=False), 1)


    def test_windows_starts_a_detached_pythonw_without_services(self):
        popen, ran = mock.Mock(), []
        answers = iter([False, True])
        with mock.patch("nina.config.gui_python", return_value="C:/py/pythonw.exe"):
            code = opener.open_chat(send=lambda m: next(answers), run=lambda c, **k: ran.append(c), popen=popen,
                                    sleep=lambda s: None, windows=True)
        self.assertEqual((code, ran), (0, []))                         # no systemctl on Windows
        self.assertEqual(popen.call_args.args[0], ["C:/py/pythonw.exe", "-m", "nina"])
        self.assertIsNone(opener.start_command(windows=True))


class DesktopUidTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_only_a_root_nina_with_a_user_display_gets_a_ui_channel(self):
        with mock.patch("os.getuid", return_value=1000):
            self.assertIsNone(sockpath.desktop_uid())
        with mock.patch("os.getuid", return_value=0), mock.patch.dict("os.environ", {"NINA_UI_UID": "1000"}):
            self.assertEqual(sockpath.desktop_uid(), 1000)
        with mock.patch("os.getuid", return_value=0), mock.patch.dict("os.environ", {"NINA_UI_UID": "0"}):
            self.assertIsNone(sockpath.desktop_uid())

    def test_clients_also_try_the_ui_channel(self):
        with mock.patch("os.getuid", return_value=1000, create=True), mock.patch.dict("os.environ", {"XDG_RUNTIME_DIR": ""}):
            self.assertIn("/run/user/1000/nina-ui.sock", sockpath.socket_paths(windows=False))


class UiChannelTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Unix sockets")
    def test_the_restricted_channel_only_accepts_chat_and_show(self):
        import os
        import tempfile
        import threading
        import time

        from tests.support import app

        from nina import app as nina_app

        got = []

        class FakeNina:
            def handle_command(self, line):
                got.append(line)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ui.sock")
            with mock.patch("nina.sockpath.desktop_uid", return_value=os.getuid() or 1), \
                 mock.patch("nina.sockpath.ui_socket_path", return_value=path), \
                 mock.patch("os.chown"), mock.patch.object(nina_app.Path, "is_dir", return_value=True):
                server = nina_app.start_ui_channel(FakeNina())
            self.assertIsNotNone(server)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            t = threading.Thread(target=lambda: sockpath.send_line(path, b"sleep\nask rm -rf /\nchat\nshow\nsay hi\n", 2))
            t.start()
            end = time.monotonic() + 10                 # generous under load; the loop ends as soon as both arrive
            while time.monotonic() < end and len(got) < 2:
                app.processEvents()
                time.sleep(0.01)
            t.join()
            app.processEvents()
            server.close()
        self.assertEqual(got, ["chat", "show"])
