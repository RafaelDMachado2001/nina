import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

from nina import sockpath


class PipeNameTest(unittest.TestCase):
    def test_name_is_per_user_and_safe(self):
        self.assertEqual(sockpath.compute_pipe_name("rafael"), "nina-rafael")
        self.assertEqual(sockpath.compute_pipe_name("DOM\\rafael m"), "nina-DOM_rafael_m")
        self.assertEqual(sockpath.compute_pipe_name(""), "nina-user")

    def test_windows_has_a_single_address_without_a_path(self):
        paths = sockpath.socket_paths(windows=True)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].startswith("nina-"))
        self.assertNotIn("\\", paths[0])
        self.assertNotIn("/", paths[0])


@unittest.skipIf(sys.platform == "win32", "Unix sockets")
class UnixSendLineTest(unittest.TestCase):
    def test_unix_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            got = []

            def accept():
                conn, _ = server.accept()
                got.append(conn.recv(100))
                conn.close()

            t = threading.Thread(target=accept)
            t.start()
            sockpath.send_line(path, b"say hi\n", windows=False)
            t.join(2)
            server.close()
            self.assertEqual(got, [b"say hi\n"])

    def test_unix_without_listener_raises_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                sockpath.send_line(os.path.join(tmp, "none.sock"), b"x", windows=False)



class SendLineTest(unittest.TestCase):
    def test_windows_writes_to_the_named_pipe(self):
        opened = mock.mock_open()
        with mock.patch("builtins.open", opened):
            sockpath.send_line("nina-rafael", b"say hi\n", windows=True)
        opened.assert_called_once_with("\\\\.\\pipe\\nina-rafael", "wb", buffering=0)
        opened().write.assert_called_once_with(b"say hi\n")

    def test_windows_without_listener_raises_oserror(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            with self.assertRaises(OSError):
                sockpath.send_line("nina-rafael", b"x", windows=True)


if __name__ == "__main__":
    unittest.main()
