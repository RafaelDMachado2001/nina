"""Remembers managed agents between runs, so a conversation can be resumed after NINA restarts.

Only what is needed to resume is kept: the role id, folder, account, Claude session id and a short tail of the
visible log. The file lives in the state directory, never in the project, and is written atomically.
"""
import json
import os
from pathlib import Path

MAX_AGENTS = 30
MAX_LOG = 200


def default_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "managed.json"


def _clean_log(raw) -> list:
    out = []
    for entry in raw or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            out.append((str(entry[0]), str(entry[1])))
    return out[-MAX_LOG:]


def load(path: Path | None = None) -> list:
    """Saved agents, oldest first. A missing or damaged file yields an empty list."""
    try:
        data = json.loads((path or default_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("agents") if isinstance(data, dict) else None
    out = []
    for a in items or []:
        if isinstance(a, dict) and a.get("role") and a.get("session") and a.get("cwd"):
            out.append({"role": str(a["role"]), "cwd": str(a["cwd"]), "account": str(a.get("account") or ""),
                        "session": str(a["session"]), "cost": float(a.get("cost") or 0.0),
                        "log": _clean_log(a.get("log"))})
    return out[-MAX_AGENTS:]


def save(agents: list, path: Path | None = None) -> None:
    """Replace the file with `agents` (same shape `load` returns). Errors are ignored: this is only a convenience."""
    target = path or default_path()
    keep = [{**a, "log": list(a.get("log") or [])[-MAX_LOG:]} for a in agents if a.get("session")][-MAX_AGENTS:]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agents": keep}, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError:
        pass
