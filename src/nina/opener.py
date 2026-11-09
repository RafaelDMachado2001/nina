"""`nina open`: bring the chat up from anywhere, starting NINA first when she is closed.

This is what the global shortcut runs. NINA often runs as root (a system service, so she can use root's Claude
accounts) while the shortcut runs as you: a closed NINA is started through `pkexec`, which asks for your password,
and an open one is reached through the restricted channel in your runtime directory.
"""
import os
import subprocess
import sys
import time

from nina import config, sockpath

SERVICE = "nina.service"


def _ok(cmd: list, run=subprocess.run) -> bool:
    try:
        return run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_command(run=subprocess.run, uid: int | None = None, windows: bool | None = None) -> list | None:
    """How to start NINA here: the system service (asking for a password unless root), your user service, or None.
    Windows has no services for her: always None (a new process)."""
    if config.WINDOWS if windows is None else windows:
        return None
    uid = os.getuid() if uid is None else uid
    if _ok(["systemctl", "cat", SERVICE], run):
        return ["systemctl", "start", SERVICE] if uid == 0 else ["pkexec", "systemctl", "start", SERVICE]
    if _ok(["systemctl", "--user", "cat", SERVICE], run):
        return ["systemctl", "--user", "start", SERVICE]
    return None


def open_chat(send=sockpath.send_message, run=subprocess.run, popen=subprocess.Popen, sleep=time.sleep,
              timeout: float = 45.0, uid: int | None = None, windows: bool | None = None) -> int:
    if send("chat"):
        return 0
    cmd = start_command(run, uid, windows)
    try:
        if cmd is not None:
            if run(cmd, timeout=120).returncode != 0:
                print("NINA: could not start the service.", file=sys.stderr)
                return 1
        else:
            # pythonw.exe and DETACHED_PROCESS on Windows: no console window comes up with her.
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            popen([config.gui_python(), "-m", "nina"], start_new_session=True, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"NINA: could not start: {e}", file=sys.stderr)
        return 1
    waited = 0.0
    while waited < timeout:                   # she needs a few seconds to come up
        if send("chat"):
            return 0
        sleep(0.5)
        waited += 0.5
    print("NINA: started, but the chat did not answer.", file=sys.stderr)
    return 1
