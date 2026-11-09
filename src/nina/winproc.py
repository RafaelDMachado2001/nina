"""Windows: child processes of a windowless NINA must not pop up console windows.

NINA runs under pythonw.exe, which has no console. Any console program it starts (the `claude` CLI behind managed
agents and the chat, which the Agent SDK spawns without creation flags) would then get a new, visible and empty
console window. Popen gets CREATE_NO_WINDOW by default; a caller that asks for a console (CREATE_NEW_CONSOLE, the
terminal that opens Claude Code) or for none (DETACHED_PROCESS) keeps what it asked for.
"""
import subprocess

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008


def flags_for(creationflags: int) -> int:
    if creationflags & (CREATE_NEW_CONSOLE | DETACHED_PROCESS | CREATE_NO_WINDOW):
        return creationflags
    return creationflags | CREATE_NO_WINDOW


def hide_child_consoles(popen_class=subprocess.Popen) -> bool:
    """Patch Popen once (idempotent). Returns True when it changed something."""
    if getattr(popen_class, "_nina_no_window", False):
        return False
    original = popen_class.__init__

    def __init__(self, *args, creationflags=0, **kwargs):
        original(self, *args, creationflags=flags_for(creationflags), **kwargs)

    popen_class.__init__ = __init__
    popen_class._nina_no_window = True
    return True
