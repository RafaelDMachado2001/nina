"""Open Claude Code in a terminal emulator, and start NINA with the desktop session (autostart)."""
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from nina import config
from nina.i18n import _

TERMINALS = ["gnome-terminal", "ptyxis", "kgx", "konsole", "xfce4-terminal", "tilix",
             "kitty", "alacritty", "wezterm", "x-terminal-emulator", "xterm"]


def terminal_command(term: str, shell_cmd: str) -> list | None:
    bash = ["bash", "-lic", shell_cmd]
    table = {
        "gnome-terminal": ["gnome-terminal", "--", *bash],
        "ptyxis": ["ptyxis", "--new-window", "--", *bash],
        "kgx": ["kgx", "--", *bash],
        "konsole": ["konsole", "-e", *bash],
        "xfce4-terminal": ["xfce4-terminal", "-x", *bash],
        "tilix": ["tilix", "-e", shlex.join(bash)],
        "kitty": ["kitty", *bash],
        "alacritty": ["alacritty", "-e", *bash],
        "wezterm": ["wezterm", "start", "--", *bash],
        "x-terminal-emulator": ["x-terminal-emulator", "-e", *bash],
        "xterm": ["xterm", "-e", *bash],
    }
    return table.get(term)


def claude_command(slug: str, prof: dict, resume: str = "") -> str | None:
    """Shell snippet that selects the account and builds the `claude` call. None if the account does not exist."""
    adir = config.account_dir(slug)
    if slug and adir is None:
        return None
    parts = ["command", "claude"]   # `command` bypasses any `claude` shell function/alias (multi-account setups)
    if resume:
        parts += ["--resume", resume]
    for d in prof.get("add_dirs") or []:
        parts += ["--add-dir", str(Path(d).expanduser())]
    perms = Path(str(prof.get("permissions_file") or "")).expanduser()
    if prof.get("permissions_file") and perms.is_file():
        parts += ["--settings", str(perms)]
    ctx = Path(str(prof.get("context_file") or "")).expanduser()
    if prof.get("context_file") and ctx.is_file():
        try:
            parts += ["--append-system-prompt", ctx.read_text()]
        except OSError:
            pass
    env = ""
    if adir is not None:
        # Exported after the login shell profile so it wins over any export for another account.
        env = (f"export CLAUDE_CONFIG_DIR={shlex.quote(str(adir))} CLAUDE_ACCOUNT={shlex.quote(slug)}; "
               "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL; ")
    return env + shlex.join(parts)


def account_of_transcript(path: str) -> str:
    """The account slug a Claude Code transcript belongs to ('' when it is the plain ~/.claude)."""
    m = re.search(r"/accounts/([^/]+)/projects/", str(path or ""))
    return m.group(1) if m else ""


def open_claude(directory: str, preferred: str = "", slug: str = "", prof: dict | None = None,
                resume: str = "") -> str | None:
    """Open a terminal running `claude` in `directory`. Returns an error message, or None on success."""
    d = directory if Path(directory).is_dir() else str(Path.home())
    run = claude_command(slug, prof or {}, resume)
    if run is None:
        return _("The account '{slug}' does not exist in {root}. Create it with your multi-account manager.",
                 slug=slug, root=config.ACCOUNTS_ROOT)
    shell_cmd = (
        f"cd {shlex.quote(d)}; "
        f"if command -v claude >/dev/null 2>&1; then {run}; "
        "else echo 'NINA: the claude command was not found in PATH.'; "
        "echo 'Install Claude Code: https://docs.claude.com/en/docs/claude-code'; fi; "
        "exec bash"
    )
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    candidates = ([preferred] if preferred else []) + TERMINALS
    for term in candidates:
        if not shutil.which(term):
            continue
        cmd = terminal_command(term, shell_cmd)
        if cmd is None:
            continue
        term_env = env
        if term == "gnome-terminal" and os.geteuid() == 0 and shutil.which("dbus-run-session"):
            # As root, gnome-terminal hangs while activating the user's D-Bus/portal services.
            # Use a private D-Bus session without portals, and --wait so it lives as long as the terminal does.
            cmd = ["dbus-run-session", "--", "gnome-terminal", "--wait", *cmd[1:]]
            term_env = {**env, "GTK_USE_PORTAL": "0", "GIO_USE_PORTALS": "0", "GDK_BACKEND": "x11"}
            term_env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        try:
            config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            log = open(config.CONFIG_FILE.parent / "launch.log", "ab")
        except OSError:
            log = subprocess.DEVNULL
        try:
            subprocess.Popen(cmd, cwd=d, env=term_env, start_new_session=True, stdout=log, stderr=log)
            return None
        except OSError:
            continue
        finally:
            if log is not subprocess.DEVNULL:
                log.close()   # the child process keeps its own copy of the descriptor
    return _("No compatible terminal emulator was found.")


def set_autostart(enabled: bool) -> None:
    """Create or remove the freedesktop autostart entry that starts NINA with the desktop session."""
    path = config.AUTOSTART_FILE
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={config.APP_NAME}\n"
            "Comment=Pixel-art mascot for your AI coding agents\n"
            f'Exec="{sys.executable}" -m nina\n'
            f"Icon={config.ICON_FILE}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-GNOME-Autostart-Delay=5\n"
        )
    elif path.exists():
        path.unlink()
