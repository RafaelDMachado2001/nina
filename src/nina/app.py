"""Command-line entry point (`nina`): starts the mascot, talks to a running one, and manages the Claude Code hooks."""
import os
import sys
from pathlib import Path

from nina import __version__, config, i18n
from nina.i18n import _
from nina.sockpath import socket_paths

USAGE = """\
NINA {version}: a pixel-art desktop mascot for your AI coding agents.

Usage:
  nina                       start NINA (a second run just brings the running one forward)
  nina say <text>            make the running NINA say something
  nina happy [text]          make it celebrate
  nina show                  bring it to the front
  nina sleep                 send it to sleep
  nina chat                  open (or close) the chat window
  nina open                  like chat, but starts NINA first when she is closed (what the shortcut runs)
  nina ask <text>            open the chat and send it a message
  nina --install-shortcut [keys] [--command cmd]
                             global shortcut that runs `nina open` (GNOME: default <Super><Shift>n;
                             Windows: a Start Menu shortcut, default Ctrl+Alt+N)
  nina --uninstall-shortcut  remove that shortcut
  nina --connections         list the AIs NINA can use (Claude accounts and the connections you added)
  nina --test-connection <id>
                             send a one-word request through that connection
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


UI_COMMANDS = ("chat", "show")


def start_ui_channel(nina):
    from PySide6.QtNetwork import QLocalServer

    from nina.sockpath import desktop_uid, ui_socket_path
    uid = desktop_uid()
    if uid is None or not Path(f"/run/user/{uid}").is_dir():
        return None
    path = ui_socket_path(uid)
    QLocalServer.removeServer(path)
    server = QLocalServer()
    if not server.listen(path):
        print(f"NINA: could not listen on {path}: {server.errorString()}", file=sys.stderr)
        return None
    try:
        os.chown(path, uid, -1)
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"NINA: could not restrict {path}: {e}", file=sys.stderr)
        server.close()
        return None

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()

            def read(conn=conn):
                for line in bytes(conn.readAll()).decode(errors="ignore").splitlines():
                    if line.strip() in UI_COMMANDS:
                        nina.handle_command(line.strip())

            conn.readyRead.connect(read)
            conn.disconnected.connect(conn.deleteLater)
            read()

    server.newConnection.connect(on_connection)
    return server


def instance_lock():
    """Held by the running NINA for her whole life. Two NINAs started at the same moment both find nobody answering,
    and on Windows both could even listen on the same pipe name: only the one that takes this lock goes on.
    None when another live process holds it (a crashed NINA's lock is taken over)."""
    from PySide6.QtCore import QLockFile
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(config.CONFIG_FILE.parent / "nina.lock"))
    lock.setStaleLockTime(0)        # stale only when its process is gone, never because of its age
    return lock if lock.tryLock(0) else None


def run_server(app) -> int:
    import time

    from PySide6.QtNetwork import QLocalServer

    from nina.i18n import _
    from nina.widget import Nina

    lock = instance_lock()
    if lock is None:                # another NINA is starting right now: let it come up, then bring it forward
        for _i in range(40):
            if send_to_running("show"):
                return 0
            time.sleep(0.25)
        print("NINA: another instance holds the lock but does not answer.", file=sys.stderr)
        return 1
    app.aboutToQuit.connect(lock.unlock)

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

    # A NINA running as root inside your desktop session also listens on a restricted channel that only you can
    # reach (it is chowned to you) and that only understands "chat" and "show": enough to open her, nothing more.
    ui_server = start_ui_channel(nina)
    if ui_server is not None:
        app.aboutToQuit.connect(ui_server.close)

    nina.show()
    nina.raise_()
    nina.say(_("Hi! I'm NINA"), 3)
    return app.exec()


def list_connections(cfg: dict) -> int:
    """`nina --connections`: every connection, never a key."""
    from nina import connections
    default = cfg.get("default_connection") or ""
    for c in connections.all_connections(cfg, config.list_accounts()):
        mark = "*" if c["id"] == default else " "
        if c["kind"] == "claude":
            print(f"{mark} {c['id']:<24} claude   {c['name']}")
        else:
            key = c["key"]
            where = {"keyring": "key: keyring", "env": f"key: ${key.get('name', '')}", "none": "no key"}.get(key.get("source"), "")
            print(f"{mark} {c['id']:<24} openai   {c['name']} · {c['model']} · {c['base_url']} · {where}")
    if not default:
        print(_("Default: the Claude account chosen in NINA's menu."))
    return 0


def test_connection(cfg: dict, conn_id: str) -> int:
    """`nina --test-connection <id>`: send a one-word request and print the answer or the error."""
    from nina import connections, secrets
    from nina.agents import openai_client
    conn = connections.get(cfg, config.list_accounts(), conn_id)
    if conn is None or conn["kind"] != "openai":
        print(_("Unknown connection: {id}. See: nina --connections", id=conn_id), file=sys.stderr)
        return 1
    try:
        data = openai_client.chat(conn["base_url"], secrets.get_key(conn), conn["model"],
                                  [{"role": "user", "content": "Reply with the word OK."}], [],
                                  min(float(conn["timeout_s"]), 120), conn["headers"])
    except openai_client.ApiError as e:
        print(openai_client.friendly(e), file=sys.stderr)
        return 1
    text = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    print(_("It works. {model} answered: {text}", model=conn["model"], text=text[:200] or "…"))
    return 0


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
    if args == ["open"]:
        from nina import opener
        return opener.open_chat()
    if args and args[0] in ("--install-hooks", "--uninstall-hooks"):
        from nina import claude_hooks
        try:
            print(claude_hooks.install_hooks() if args[0] == "--install-hooks" else claude_hooks.uninstall_hooks())
        except (OSError, ValueError) as e:
            print(f"NINA: could not change {config.settings_path()}: {e}", file=sys.stderr)
            return 1
        return 0

    if args == ["--connections"]:
        return list_connections(cfg)
    if args and args[0] == "--test-connection":
        return test_connection(cfg, args[1] if len(args) > 1 else "")

    if args and args[0] in ("--install-shortcut", "--uninstall-shortcut"):
        from nina import shortcut
        try:
            if args[0] == "--uninstall-shortcut":
                print(_("Shortcut removed.") if shortcut.uninstall() else _("There was no NINA shortcut."))
            else:
                rest = args[1:]
                command = None
                if "--command" in rest:
                    i = rest.index("--command")
                    command = rest[i + 1] if i + 1 < len(rest) else None
                    rest = rest[:i] + rest[i + 2:]
                print(_("Shortcut {keys} runs: {command}", keys=shortcut.install(rest[0] if rest else shortcut.default_keys(), command),
                        command=command or shortcut.default_command()))
        except RuntimeError as e:
            print(f"NINA: {e}", file=sys.stderr)
            return 1
        return 0

    if sys.version_info < (3, 12):
        import PySide6
        if tuple(int(x) for x in PySide6.__version__.split(".")[:2]) >= (6, 12):
            print("NINA: PySide6 6.12 crashes on Python < 3.12 (reference-count bug). "
                  "Install an older one: pip install 'PySide6<6.12'", file=sys.stderr)
            return 1

    # On Ubuntu with Wayland, windows cannot position themselves or stay "always on top".
    # Running through XWayland (xcb) fixes that. Set QT_QPA_PLATFORM yourself to override.
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    if config.WINDOWS:
        from nina.winproc import hide_child_consoles
        hide_child_consoles()
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
