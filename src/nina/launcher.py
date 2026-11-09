"""Open Claude Code in a terminal emulator, and start NINA with the desktop session (autostart).

Linux: a terminal emulator running `bash -lic`. Windows: Windows Terminal (or a PowerShell console) running a small
PowerShell script, passed with `-EncodedCommand` so no quoting layer (wt.exe, CreateProcess, PowerShell) can mangle it.
"""
import base64
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
WINDOWS_TERMINALS = ["wt", "pwsh", "powershell"]
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "NINA"


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


def _claude_args(prof: dict, resume: str = "") -> list:
    args = []
    if resume:
        args += ["--resume", resume]
    for d in prof.get("add_dirs") or []:
        args += ["--add-dir", str(Path(d).expanduser())]
    perms = Path(str(prof.get("permissions_file") or "")).expanduser()
    if prof.get("permissions_file") and perms.is_file():
        args += ["--settings", str(perms)]
    ctx = Path(str(prof.get("context_file") or "")).expanduser()
    if prof.get("context_file") and ctx.is_file():
        try:
            args += ["--append-system-prompt", ctx.read_text(encoding="utf-8")]
        except OSError:
            pass
    return args


def claude_command(slug: str, prof: dict, resume: str = "") -> str | None:
    """Shell snippet that selects the account and builds the `claude` call. None if the account does not exist."""
    adir = config.account_dir(slug)
    if slug and adir is None:
        return None
    # `command` bypasses any `claude` shell function/alias (multi-account setups)
    parts = ["command", "claude", *_claude_args(prof, resume)]
    env = ""
    if adir is not None:
        # Exported after the login shell profile so it wins over any export for another account.
        env = (f"export CLAUDE_CONFIG_DIR={shlex.quote(str(adir))} CLAUDE_ACCOUNT={shlex.quote(slug)}; "
               "unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL; ")
    return env + shlex.join(parts)


def ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal (nothing inside is expanded; a quote is doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def powershell_script(directory: str, slug: str, prof: dict, resume: str = "") -> str | None:
    """PowerShell that goes to `directory`, selects the account and runs `claude`. None if the account does not exist."""
    adir = config.account_dir(slug)
    if slug and adir is None:
        return None
    lines = [f"Set-Location -LiteralPath {ps_quote(directory)}"]
    if adir is not None:
        lines += [f"$env:CLAUDE_CONFIG_DIR = {ps_quote(str(adir))}", f"$env:CLAUDE_ACCOUNT = {ps_quote(slug)}",
                  "Remove-Item Env:ANTHROPIC_API_KEY, Env:ANTHROPIC_AUTH_TOKEN, Env:ANTHROPIC_BASE_URL "
                  "-ErrorAction SilentlyContinue"]
    args = " ".join(ps_quote(a) for a in _claude_args(prof, resume))
    lines += [
        # The application itself, never a `claude` function or alias from the user's profile.
        "$nina_claude = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1",
        "if ($nina_claude) { & $nina_claude.Source " + args + " } else {",
        "  Write-Host 'NINA: the claude command was not found in PATH.'",
        "  Write-Host 'Install Claude Code: https://docs.claude.com/en/docs/claude-code' }",
    ]
    return "\n".join(lines)


def encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def windows_terminal_command(term: str, directory: str, script: str) -> list | None:
    """Command line that opens `term` (wt, pwsh or powershell) running `script` and staying open afterwards."""
    shell = "pwsh" if term == "pwsh" or (term == "wt" and shutil.which("pwsh")) else "powershell"
    ps = [shell, "-NoLogo", "-NoExit", "-EncodedCommand", encode_powershell(script)]
    if term == "wt":
        return ["wt", "-w", "new", "new-tab", "--title", "Claude Code", "-d", directory, *ps]
    if term in ("pwsh", "powershell"):
        return ps
    return None


def account_of_transcript(path: str) -> str:
    """The account slug a Claude Code transcript belongs to ('' when it is the plain ~/.claude)."""
    m = re.search(r"[/\\]accounts[/\\]([^/\\]+)[/\\]projects[/\\]", str(path or ""))
    return m.group(1) if m else ""


def _launch_log():
    try:
        config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        return open(config.CONFIG_FILE.parent / "launch.log", "ab")
    except OSError:
        return subprocess.DEVNULL


def open_claude(directory: str, preferred: str = "", slug: str = "", prof: dict | None = None,
                resume: str = "", windows: bool | None = None) -> str | None:
    """Open a terminal running `claude` in `directory`. Returns an error message, or None on success."""
    windows = config.WINDOWS if windows is None else windows
    d = directory if Path(directory).is_dir() else str(Path.home())
    if windows:
        return _open_claude_windows(d, preferred, slug, prof or {}, resume)
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
        log = _launch_log()
        try:
            subprocess.Popen(cmd, cwd=d, env=term_env, start_new_session=True, stdout=log, stderr=log)
            return None
        except OSError:
            continue
        finally:
            if log is not subprocess.DEVNULL:
                log.close()   # the child process keeps its own copy of the descriptor
    return _("No compatible terminal emulator was found.")


def _open_claude_windows(d: str, preferred: str, slug: str, prof: dict, resume: str) -> str | None:
    script = powershell_script(d, slug, prof, resume)
    if script is None:
        return _("The account '{slug}' does not exist in {root}. Create it with your multi-account manager.",
                 slug=slug, root=config.ACCOUNTS_ROOT)
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    candidates = ([preferred] if preferred else []) + WINDOWS_TERMINALS
    for term in candidates:
        cmd = windows_terminal_command(term, d, script)
        if cmd is None or not shutil.which(cmd[0]):
            continue
        # A console program started from pythonw.exe needs a console of its own; wt.exe is a GUI program.
        flags = 0 if term == "wt" else getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            subprocess.Popen(cmd, cwd=d, env=env, creationflags=flags, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return None
        except OSError:
            continue
    return _("No compatible terminal emulator was found.")


def autostart_command() -> str:
    return f'"{config.gui_python()}" -m nina'


def autostart_enabled(windows: bool | None = None) -> bool:
    windows = config.WINDOWS if windows is None else windows
    if not windows:
        return config.AUTOSTART_FILE.exists()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool, windows: bool | None = None) -> None:
    """Start NINA when you log in: a freedesktop autostart entry, or on Windows a value in the user's Run key."""
    windows = config.WINDOWS if windows is None else windows
    if windows:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return
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
            "X-GNOME-Autostart-Delay=5\n",
            encoding="utf-8",
        )
    elif path.exists():
        path.unlink()
