"""Paths, defaults and the per-user configuration (`~/.config/nina/config.json`). No Qt, standard library only."""
import json
import os
import sys
from pathlib import Path

APP_NAME = "NINA"
WINDOWS = sys.platform == "win32"
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_FILE = XDG_CONFIG / "nina" / "config.json"
AUTOSTART_FILE = XDG_CONFIG / "autostart" / "nina.desktop"
ACCOUNTS_ROOT = Path.home() / ".claude" / "accounts"   # multi-account layout: one config dir per account
ICON_FILE = Path(__file__).resolve().parent / "assets" / "nina.png"

DEFAULTS = {
    "default_dir": str(Path.home()),
    "recent": [],
    "pos": None,
    "scale": 4,
    "terminal": "",          # e.g. "gnome-terminal" (Windows: "wt", "pwsh" or "powershell") to force a terminal
    "sleep_minutes": 10,
    "language": "en",        # "en", "pt-BR" or "auto" (from $LANG)
    # Active account: a folder in ~/.claude/accounts/<account> used as CLAUDE_CONFIG_DIR. Empty = plain ~/.claude.
    "account": "",
    # Per-account profile (optional): default_dir, add_dirs, context_file, permissions_file.
    "profiles": {},
    "claude_bin": "",        # path of the `claude` binary for managed agents; empty = auto-detect
}


def gui_python(executable: str | None = None) -> str:
    """The interpreter to start NINA with: on Windows `pythonw.exe` (next to the current one) so no console opens."""
    exe = executable or sys.executable
    p = Path(exe)
    if p.name.lower() == "python.exe" and (p.parent / "pythonw.exe").exists():
        return str(p.parent / "pythonw.exe")
    return exe


def plugins_dir() -> Path:
    return CONFIG_FILE.parent / "plugins"


def roles_dir() -> Path:
    return CONFIG_FILE.parent / "roles"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"NINA: could not save the configuration: {e}", file=sys.stderr)


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def list_accounts() -> list:
    """Accounts discovered in ~/.claude/accounts/* (nothing hard-coded)."""
    try:
        return sorted(d.name for d in ACCOUNTS_ROOT.iterdir() if d.is_dir())
    except OSError:
        return []


def account_dir(slug: str) -> Path | None:
    d = ACCOUNTS_ROOT / slug if slug else None
    return d if d is not None and d.is_dir() else None


def profile_of(cfg: dict, slug: str | None = None) -> dict:
    p = (cfg.get("profiles") or {}).get(slug or cfg.get("account"), {})
    return p if isinstance(p, dict) else {}


def default_dir_of(cfg: dict, slug: str | None = None) -> str:
    return str(Path(profile_of(cfg, slug).get("default_dir") or cfg["default_dir"]).expanduser())


def settings_path(cfg: dict | None = None) -> Path:
    """Real settings.json of the active account. Symlinks are resolved (settings.json is often shared between
    accounts) so that writing it never replaces the link with a plain file."""
    cfg = cfg or load_config()
    base = account_dir(cfg.get("account", "")) or Path.home() / ".claude"
    return Path(os.path.realpath(base / "settings.json"))
