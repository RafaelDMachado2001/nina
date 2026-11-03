"""Install/uninstall NINA's Claude Code hooks in the active account's settings.json (merge, never overwrite)."""
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

from nina import config
from nina.i18n import _

# PostToolUse only clears the "waiting" alert once an approved tool finishes; SessionStart/Subagent* feed the agents panel.
HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop",
               "SubagentStart", "SubagentStop", "SessionEnd"]
HOOK_TOOL_EVENTS = {"PreToolUse", "PostToolUse"}


def hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -m nina.hook"


def _is_nina_hook(handler) -> bool:
    """NINA's entries are recognised by the module name (`nina.hook`) or the legacy script name."""
    command = str(handler.get("command", "")) if isinstance(handler, dict) else ""
    return "nina.hook" in command or "nina_hook.py" in command


def _read_settings() -> dict:
    path = config.settings_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text())   # ValueError if the JSON is invalid
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def _write_settings(data: dict) -> None:
    path = config.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.nina-bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def hooks_installed() -> bool:
    try:
        hooks = _read_settings().get("hooks", {})
    except (OSError, ValueError):
        return False
    return all(any(_is_nina_hook(h) for g in hooks.get(ev, []) if isinstance(g, dict)
                   for h in g.get("hooks", [])) for ev in HOOK_EVENTS)


def install_hooks() -> str:
    """Merge NINA's hooks into settings.json (idempotent; legacy entries are updated). Returns a status message."""
    data = _read_settings()
    hooks = data.setdefault("hooks", {})
    entry = {"type": "command", "command": hook_command(), "timeout": 2}
    changed = False
    for ev in HOOK_EVENTS:
        groups = hooks.setdefault(ev, [])
        found = False
        for g in groups:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks", []):
                if _is_nina_hook(h):
                    found = True
                    if h != entry:
                        h.clear()
                        h.update(entry)
                        changed = True
        if not found:
            group = {"hooks": [dict(entry)]}
            if ev in HOOK_TOOL_EVENTS:
                group["matcher"] = "*"
            groups.append(group)
            changed = True
    if not changed:
        return _("NINA's hooks were already installed.")
    _write_settings(data)
    return _("Hooks installed in {path}. Start a new claude session (or use /hooks to check).", path=config.settings_path())


def uninstall_hooks() -> str:
    data = _read_settings()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return _("No NINA hooks found.")
    changed = False
    for ev in list(hooks):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                kept = [h for h in g["hooks"] if not _is_nina_hook(h)]
                if len(kept) != len(g["hooks"]):
                    changed = True
                    if not kept:
                        continue
                    g["hooks"] = kept
            kept_groups.append(g)
        if kept_groups:
            hooks[ev] = kept_groups
        else:
            del hooks[ev]
    if not hooks:
        del data["hooks"]
    if not changed:
        return _("No NINA hooks found.")
    _write_settings(data)
    return _("NINA's hooks were removed.")
