"""Conversations of agents on OpenAI-compatible connections, kept by NINA so they can be resumed after a restart
(Claude keeps its own sessions; these APIs keep nothing). One private JSON file per session. Standard library only."""
import json
import os
import re
import time
import uuid
from pathlib import Path

ID_RE = re.compile(r"[0-9a-f]{32}")


def folder() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "sessions"


def new_id() -> str:
    return uuid.uuid4().hex


def path_of(session_id: str) -> Path | None:
    return folder() / f"{session_id}.json" if ID_RE.fullmatch(str(session_id or "")) else None


def save(session_id: str, messages: list) -> None:
    path = path_of(session_id)
    if path is None:
        raise ValueError("invalid session id")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "saved": time.time(), "messages": messages}, ensure_ascii=False),
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load(session_id: str) -> list | None:
    path = path_of(session_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    messages = data.get("messages") if isinstance(data, dict) else None
    return messages if isinstance(messages, list) else None


def prune(keep_days: float, keep: set) -> int:
    """Delete sessions not used for `keep_days` days, except the ids in `keep`. Returns how many were deleted."""
    cutoff, removed = time.time() - keep_days * 86400, 0
    try:
        files = list(folder().glob("*.json"))
    except OSError:
        return 0
    for f in files:
        try:
            if f.stem not in keep and f.stat().st_mtime <= cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
