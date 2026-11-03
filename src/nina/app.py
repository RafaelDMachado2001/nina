"""Command-line entry point (`nina`): starts the mascot, talks to a running one, and manages the Claude Code hooks."""
import os
import sys
from pathlib import Path

from nina import __version__, config, i18n
from nina.sockpath import socket_paths

USAGE = """\
NINA {version}: a pixel-art desktop mascot for your AI coding agents.

Usage:
  nina                       start NINA (a second run just brings the running one forward)
  nina say <text>            make the running NINA say something
  nina happy [text]          make it celebrate
  nina show                  bring it to the front
  nina sleep                 send it to sleep
  nina --install-hooks       connect NINA to Claude Code (merges hooks into settings.json)
  nina --uninstall-hooks     remove NINA's hooks
  nina --export [dir]        write the icon and a sheet with every frame
  nina --version
"""


def send_to_running(message: str) -> bool:
    from PySide6.QtNetwork import QLocalSocket
    for path in socket_paths():
        sock = QLocalSocket()
        sock.connectToServer(path)
        if not sock.waitForConnected(300):
            continue
        sock.write((message + "\n").encode())
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def run_server(app) -> int:
    from PySide6.QtNetwork import QLocalServer

    from nina.i18n import _
    from nina.widget import Nina

    sock_path = socket_paths()[0]
    QLocalServer.removeServer(sock_path)
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(sock_path):
        print(f"NINA: could not listen on {sock_path}: {server.errorString()}", file=sys.stderr)
        return 1

    nina = Nina()
    app.aboutToQuit.connect(nina.shutdown)

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            buf = bytearray()

            def read(conn=conn, buf=buf, final=False):
                buf += bytes(conn.readAll())
                *lines, rest = bytes(buf).split(b"\n")
                buf[:] = rest
                if final and rest:
                    lines.append(rest)
                    buf.clear()
                for line in lines:
                    nina.handle_command(line.decode(errors="ignore"))

            conn.readyRead.connect(read)
            conn.disconnected.connect(lambda read=read: read(final=True))
            conn.disconnected.connect(conn.deleteLater)
            read()   # the client may already have sent everything before the signal was connected

    server.newConnection.connect(on_connection)

    nina.show()
    nina.raise_()
    nina.say(_("Hi! I'm NINA"), 3)
    return app.exec()


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg = config.load_config()
    i18n.set_language(cfg.get("language", "en"))

    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE.format(version=__version__))
        return 0
    if args and args[0] == "--version":
        print(f"nina {__version__}")
        return 0
    if args and args[0] in ("--install-hooks", "--uninstall-hooks"):
        from nina import claude_hooks
        try:
            print(claude_hooks.install_hooks() if args[0] == "--install-hooks" else claude_hooks.uninstall_hooks())
        except (OSError, ValueError) as e:
            print(f"NINA: could not change {config.settings_path()}: {e}", file=sys.stderr)
            return 1
        return 0

    # On Ubuntu with Wayland, windows cannot position themselves or stay "always on top".
    # Running through XWayland (xcb) fixes that. Set QT_QPA_PLATFORM yourself to override.
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])
    app.setApplicationName(config.APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    if args and args[0] == "--export":
        from nina.sprites import export_images
        export_images(Path(args[1]) if len(args) > 1 else Path.cwd())
        return 0
    if args:
        if send_to_running(" ".join(args)):
            return 0
        print("NINA is not running.", file=sys.stderr)
        return 1
    if send_to_running("show"):
        return 0   # a NINA is already running
    return run_server(app)


if __name__ == "__main__":
    sys.exit(main())
