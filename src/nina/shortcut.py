"""A global keyboard shortcut that opens the chat. GNOME only: it registers a custom keybinding that runs `nina open`.

Qt cannot grab keys globally on X11/XWayland without extra dependencies, and a desktop-level binding also works on
Wayland, so the desktop does the grabbing and NINA only answers the command.
"""
import ast
import shlex
import subprocess
import sys

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM_SCHEMA = SCHEMA + ".custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SLUG = "nina-chat"
PATH = f"{BASE}{SLUG}/"
DEFAULT_KEYS = "<Super><Shift>n"      # plain <Super>n is GNOME's "focus the active notification"


def default_command() -> str:
    return f"{shlex.quote(sys.executable)} -m nina open"


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
    paths = _parse_list(_gsettings("get", SCHEMA, "custom-keybindings", run=run))
    if PATH not in paths:
        return False
    _write_list([p for p in paths if p != PATH], run)
    item = f"{ITEM_SCHEMA}:{PATH}"
    for key in ("name", "command", "binding"):
        _gsettings("reset", item, key, run=run)
    return True
