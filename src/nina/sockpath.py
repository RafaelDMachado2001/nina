"""Where the NINA control channel lives. Standard library only: the (latency-critical) hook imports this too.

On Linux and macOS it is a Unix socket. On Windows it is a named pipe: Qt's QLocalServer/QLocalSocket take a plain
name there and map it to `\\\\.\\pipe\\<name>`, so the same Qt code serves both.
"""
import getpass
import os
import re
import socket
import sys
from pathlib import Path

SOCKET_PATH_MAX = 100   # a Unix socket path is limited to ~107 bytes
PIPE_PREFIX = "\\\\.\\pipe\\"


def compute_sock_path(runtime_dir: str, uid: int) -> str:
    """Primary socket path: `$XDG_RUNTIME_DIR/nina.sock`, or `/tmp/nina-<uid>.sock` if that is unusable or too long."""
    if runtime_dir and Path(runtime_dir).is_dir():
        candidate = str(Path(runtime_dir) / "nina.sock")
        if len(candidate.encode()) <= SOCKET_PATH_MAX:
            return candidate
    return f"/tmp/nina-{uid}.sock"


def compute_pipe_name(user: str) -> str:
    """Windows pipe name, one per user, so two logged-in users do not collide. Pipe names cannot hold `\\`."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user or "") or "user"
    return f"nina-{safe}"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "")


def socket_paths(windows: bool | None = None) -> list:
    """Every address a client should try, in order. NINA listens on the first one."""
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        return [compute_pipe_name(_current_user())]
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    uid = os.getuid()
    paths = [compute_sock_path(runtime, uid)]
    if runtime and Path(runtime).is_dir():
        paths.append(str(Path(runtime) / "nina.sock"))
    paths.append(f"/tmp/nina-{uid}.sock")
    paths.append(ui_socket_path(uid))        # reaches a NINA that runs as root on this user's desktop
    return list(dict.fromkeys(paths))


def send_message(message: str, timeout: float = 0.5) -> bool:
    """Deliver one command to a running NINA without starting Qt. False when none answers."""
    for path in socket_paths():
        try:
            send_line(path, (message + "\n").encode(), timeout=timeout)
            return True
        except OSError:
            continue
    return False


def desktop_uid() -> int | None:
    """The user of the graphical session, when NINA runs as root inside it: the owner of the display's auth file
    (or NINA_UI_UID). None when NINA runs as that user itself or when it cannot tell."""
    if os.name != "posix" or os.getuid() != 0:
        return None
    try:
        uid = int(os.environ["NINA_UI_UID"]) if os.environ.get("NINA_UI_UID") else os.stat(os.environ.get("XAUTHORITY", "")).st_uid
    except (OSError, ValueError):
        return None
    return uid if uid != 0 else None


def ui_socket_path(uid: int) -> str:
    """A second, restricted channel in the desktop user's runtime dir: a root NINA lets that user toggle the chat."""
    return f"/run/user/{uid}/nina-ui.sock"


def send_line(address: str, data: bytes, timeout: float = 0.3, windows: bool | None = None) -> None:
    """Deliver `data` to the running NINA at `address`. Raises OSError if nobody is listening there."""
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        # A byte-mode named pipe opens like a file. A missing pipe raises FileNotFoundError at once.
        with open(PIPE_PREFIX + address, "wb", buffering=0) as pipe:
            pipe.write(data)
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(address)
        s.sendall(data)
    finally:
        s.close()
