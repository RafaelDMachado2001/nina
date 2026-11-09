"""A global keyboard shortcut that opens the chat: the desktop grabs the keys and runs `nina open`.

GNOME: a custom keybinding (Qt cannot grab keys globally on X11/XWayland without extra dependencies, and a
desktop-level binding also works on Wayland). Windows: a Start Menu shortcut with a hotkey, which Explorer itself
listens for (Windows only honours hotkeys of Start Menu and desktop shortcuts, and they need Ctrl and/or Alt).
"""
import ast
import base64
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from nina import config

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM_SCHEMA = SCHEMA + ".custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SLUG = "nina-chat"
PATH = f"{BASE}{SLUG}/"
DEFAULT_KEYS = "<Super><Shift>n"      # plain <Super>n is GNOME's "focus the active notification"


WINDOWS_DEFAULT_KEYS = "Ctrl+Alt+N"
LNK_NAME = "NINA Chat.lnk"


def default_keys(windows: bool | None = None) -> str:
    return WINDOWS_DEFAULT_KEYS if (config.WINDOWS if windows is None else windows) else DEFAULT_KEYS


def default_command() -> str:
    if config.WINDOWS:
        return f'"{config.gui_python()}" -m nina open'
    return f"{shlex.quote(sys.executable)} -m nina open"


# ---- Windows ----------------------------------------------------------------------------------------------
def programs_dir() -> Path:
    """The user's Start Menu programs folder."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def windows_hotkey(keys: str) -> str:
    """`<Ctrl><Alt>n`, `ctrl+alt+n` or `Ctrl+Alt+N` -> `CTRL+ALT+N` (the WScript.Shell format)."""
    names = {"control": "CTRL", "ctrl": "CTRL", "primary": "CTRL", "alt": "ALT", "shift": "SHIFT"}
    parts = [p for p in re.split(r"[<>+\s]+", keys.strip()) if p]
    if not parts:
        raise RuntimeError("empty shortcut")
    *mods, key = parts
    out = []
    for m in mods:
        name = names.get(m.lower())
        if name is None:
            raise RuntimeError(f"Windows shortcuts can only use Ctrl, Alt and Shift, not {m!r}")
        if name not in out:
            out.append(name)
    if "CTRL" not in out and "ALT" not in out:
        raise RuntimeError("a Windows shortcut needs Ctrl or Alt")
    return "+".join([m for m in ("CTRL", "SHIFT", "ALT") if m in out] + [key.upper()])


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_powershell(script: str, run=subprocess.run) -> str:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
           base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
    try:
        out = run(cmd, capture_output=True, text=True, timeout=30,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"PowerShell is not available: {e}") from e
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "").strip() or "PowerShell failed")
    return (out.stdout or "").strip()


def icon_file() -> Path | None:
    """NINA's icon as a square .ico (Windows shortcuts cannot use a .png). None if Qt cannot write it."""
    target = config.CONFIG_FILE.parent / "nina.ico"
    if target.exists():
        return target
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter
        src = QImage(str(config.ICON_FILE)).scaled(256, 256, Qt.KeepAspectRatio, Qt.FastTransformation)
        img = QImage(256, 256, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.drawImage((256 - src.width()) // 2, (256 - src.height()) // 2, src)
        p.end()
        target.parent.mkdir(parents=True, exist_ok=True)
        return target if img.save(str(target), "ICO") else None
    except Exception:
        return None


def make_lnk(path: Path, target: str, arguments: str, hotkey: str = "", description: str = "",
             icon: Path | None = None, run=subprocess.run) -> None:
    """Create or replace a Windows shortcut (.lnk) through the WScript.Shell COM object."""
    lines = [
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + ps_quote(str(path)) + ")",
        "$s.TargetPath = " + ps_quote(target),
        "$s.Arguments = " + ps_quote(arguments),
        "$s.WorkingDirectory = " + ps_quote(str(Path.home())),
        "$s.Description = " + ps_quote(description),
        "$s.Hotkey = " + ps_quote(hotkey),
    ]
    if icon is not None:
        lines.append("$s.IconLocation = " + ps_quote(f"{icon},0"))
    lines.append("$s.Save()")
    path.parent.mkdir(parents=True, exist_ok=True)
    run_powershell("\n".join(lines), run=run)


def install_windows(keys: str = WINDOWS_DEFAULT_KEYS, run=subprocess.run) -> str:
    hotkey = windows_hotkey(keys or WINDOWS_DEFAULT_KEYS)
    make_lnk(programs_dir() / LNK_NAME, config.gui_python(), "-m nina open", hotkey,
             "Open or close the NINA chat", icon_file(), run=run)
    return hotkey


def uninstall_windows() -> bool:
    lnk = programs_dir() / LNK_NAME
    if not lnk.exists():
        return False
    lnk.unlink()
    return True


def _gsettings(*args: str, run=subprocess.run) -> str:
    try:
        out = run(["gsettings", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"gsettings is not available: {e}") from e
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gsettings failed")
    return out.stdout.strip()


def _parse_list(text: str) -> list:
    text = text.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _write_list(paths: list, run) -> None:
    _gsettings("set", SCHEMA, "custom-keybindings", repr(paths), run=run)


def install(keys: str = DEFAULT_KEYS, command: str | None = None, run=subprocess.run) -> str:
    """Create or update the shortcut (idempotent) and keep every other custom shortcut as it was."""
    if config.WINDOWS:
        if command:
            raise RuntimeError("--command is not supported on Windows")
        return install_windows(keys if keys != DEFAULT_KEYS else WINDOWS_DEFAULT_KEYS, run=run)
    keys = keys.strip() or DEFAULT_KEYS
    paths = _parse_list(_gsettings("get", SCHEMA, "custom-keybindings", run=run))
    if PATH not in paths:
        _write_list(paths + [PATH], run)
    item = f"{ITEM_SCHEMA}:{PATH}"
    _gsettings("set", item, "name", "NINA", run=run)
    _gsettings("set", item, "command", command or default_command(), run=run)
    _gsettings("set", item, "binding", keys, run=run)
    return keys


def uninstall(run=subprocess.run) -> bool:
    if config.WINDOWS:
        return uninstall_windows()
    paths = _parse_list(_gsettings("get", SCHEMA, "custom-keybindings", run=run))
    if PATH not in paths:
        return False
    _write_list([p for p in paths if p != PATH], run)
    item = f"{ITEM_SCHEMA}:{PATH}"
    for key in ("name", "command", "binding"):
        _gsettings("reset", item, key, run=run)
    return True
