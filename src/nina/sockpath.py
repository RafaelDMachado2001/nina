"""Where the NINA control socket lives. Standard library only: the (latency-critical) hook imports this too."""
import os
from pathlib import Path

SOCKET_PATH_MAX = 100   # a Unix socket path is limited to ~107 bytes


def compute_sock_path(runtime_dir: str, uid: int) -> str:
    """Primary socket path: `$XDG_RUNTIME_DIR/nina.sock`, or `/tmp/nina-<uid>.sock` if that is unusable or too long."""
    if runtime_dir and Path(runtime_dir).is_dir():
        candidate = str(Path(runtime_dir) / "nina.sock")
        if len(candidate.encode()) <= SOCKET_PATH_MAX:
            return candidate
    return f"/tmp/nina-{uid}.sock"


def socket_paths() -> list:
    """Every path a client should try, in order. NINA listens on the first one."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    uid = os.getuid()
    paths = [compute_sock_path(runtime, uid)]
    if runtime and Path(runtime).is_dir():
        paths.append(str(Path(runtime) / "nina.sock"))
    paths.append(f"/tmp/nina-{uid}.sock")
    return list(dict.fromkeys(paths))
