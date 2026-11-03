"""
Agent and event registry for NINA (SQLite). Standard library only, no Qt: testable in isolation.

Fed by Claude Code hooks (through NINA): every session is an agent, and every subagent
(an `agent_id` in the events) is a child of the session that spawned it. Nothing here changes or controls agents.
Texts are length-limited and go through `redact` before being stored.
"""
import os
import re
import sqlite3
import time
from pathlib import Path

from nina.i18n import _

ACTIVE = ("working", "waiting")
STALE_AFTER_S = 180          # "working" with no events for this long becomes "stale" (no signal)
KEEP_DAYS = 7
MAX_TEXT = 200

_SECRET_TOKEN = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{12,})")
_SECRET_KV = re.compile(
    r"(?i)((?:token|secret|password|passwd|senha|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{4,})")


def redact(text: str) -> str:
    """Mask common secrets (keys, tokens, passwords) before storing or displaying text."""
    text = _SECRET_TOKEN.sub("[hidden]", text or "")
    return _SECRET_KV.sub(lambda m: m.group(1) + "[hidden]", text)


def clip(text, limit: int = MAX_TEXT) -> str:
    text = redact(" ".join(str(text or "").split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def default_db_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "agents.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    parent_id      TEXT,
    kind           TEXT NOT NULL,              -- session | subagent
    agent_type     TEXT,
    title          TEXT,
    cwd            TEXT,
    transcript     TEXT,
    status         TEXT NOT NULL DEFAULT 'idle',  -- idle | working | waiting | done | ended
    activity       TEXT,
    started_at     REAL NOT NULL,
    last_event_at  REAL NOT NULL,
    ended_at       REAL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    agent_id TEXT NOT NULL,
    type     TEXT NOT NULL,
    tool     TEXT,
    summary  TEXT
);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id, id);
"""


class AgentStore:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            path = default_db_path()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open()
        except sqlite3.DatabaseError:
            # Corrupt database: set the bad file aside (never deleted) and start over with a fresh one.
            self._set_aside()
            self._open()
        self._writes = 0
        self.purge()

    def _open(self) -> None:
        self.db = sqlite3.connect(self.path, timeout=1.0)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def _set_aside(self) -> None:
        try:
            self.db.close()
        except (AttributeError, sqlite3.Error):
            pass
        if self.path == ":memory:":
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(self.path + suffix)
            if src.exists():
                src.rename(src.with_name(f"{src.name}.corrupt-{stamp}"))

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ---- writing ----------------------------------------------------------
    def _ensure(self, key: str, sid: str, kind: str, parent: str | None, atype, cwd, transcript, now: float) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO agents (id, session_id, parent_id, kind, started_at, last_event_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (key, sid, parent, kind, now, now))
        self.db.execute(
            "UPDATE agents SET last_event_at = ?, agent_type = COALESCE(?, agent_type),"
            " cwd = COALESCE(?, cwd), transcript = COALESCE(?, transcript) WHERE id = ?",
            (now, atype or None, cwd or None, transcript or None, key))

    def _set(self, key: str, status: str | None = None, activity: str | None = None,
             title: str | None = None, ended_at: float | None = None, reopen: bool = False) -> None:
        self.db.execute(
            "UPDATE agents SET status = COALESCE(?, status), activity = COALESCE(?, activity),"
            " title = CASE WHEN (title IS NULL OR title = '') THEN ? ELSE title END,"
            " ended_at = CASE WHEN ? THEN NULL ELSE COALESCE(?, ended_at) END WHERE id = ?",
            (status, activity, title or None, 1 if reopen else 0, ended_at, key))

    def _event(self, now: float, key: str, kind: str, tool=None, summary=None) -> None:
        self.db.execute("INSERT INTO events (ts, agent_id, type, tool, summary) VALUES (?, ?, ?, ?, ?)",
                        (now, key, kind, tool or None, clip(summary) if summary else None))

    def record(self, ev: dict, now: float | None = None) -> None:
        """Apply a hook event ({ev, sid, aid, atype, cwd, tp, tool, detail, prompt, last, msg, ...})."""
        name, sid = ev.get("ev"), ev.get("sid")
        if not name or not sid:
            return
        now = time.time() if now is None else now
        sid, aid = str(sid), ev.get("aid")
        key = f"{sid}/{aid}" if aid else sid
        self._ensure(sid, sid, "session", None, None, ev.get("cwd"), ev.get("tp"), now)
        if aid:
            self._ensure(key, sid, "subagent", sid, ev.get("atype"), ev.get("cwd"), ev.get("tp"), now)
        tool, detail = ev.get("tool"), clip(ev.get("detail"))
        if name == "UserPromptSubmit":
            prompt = clip(ev.get("prompt"), 120)
            self._set(sid, "working", _("Request: {text}", text=prompt) if prompt else None, prompt, reopen=True)
            self._event(now, sid, "prompt", None, prompt)
        elif name == "PreToolUse":
            self._set(key, "working", f"{tool}: {detail}" if detail else (tool or None), reopen=True)
            self._event(now, key, "tool", tool, detail)
        elif name == "PostToolUse":
            self._set(key, "working")
        elif name == "Notification":
            msg = clip(ev.get("msg"))
            self._set(key, "waiting", msg or _("Waiting for you"))
            self._event(now, key, "waiting", None, msg)
        elif name == "Stop":
            last = clip(ev.get("last"), 160)
            self._set(sid, "done", _("Done: {text}", text=last) if last else _("Done"))
            self._event(now, sid, "done", None, last)
        elif name == "SubagentStart":
            self._set(key, "working", _("Started"), reopen=True)
            self._event(now, key, "start", None, ev.get("atype"))
        elif name == "SubagentStop":
            last = clip(ev.get("last"), 160)
            self._set(key, "done", _("Done: {text}", text=last) if last else _("Done"), ended_at=now)
            self._event(now, key, "done", None, last)
        elif name == "SessionStart":
            self._event(now, sid, "session", None, _("Session started"))
        elif name == "SessionEnd":
            reason = clip(ev.get("reason"), 60)
            self._set(sid, "ended", _("Session ended"), ended_at=now)
            self.db.execute("UPDATE agents SET status = 'ended', ended_at = COALESCE(ended_at, ?)"
                            " WHERE parent_id = ?", (now, sid))
            self._event(now, sid, "end", None, reason)
        self.db.commit()
        self._writes += 1
        if self._writes % 500 == 0:
            self.purge(now)

    def purge(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        limit = now - KEEP_DAYS * 86400
        self.db.execute("DELETE FROM events WHERE agent_id IN (SELECT id FROM agents WHERE last_event_at < ?)", (limit,))
        self.db.execute("DELETE FROM agents WHERE last_event_at < ?", (limit,))
        self.db.commit()

    def clear_finished(self) -> None:
        """Remove ended sessions (with their children) and finished subagents."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM agents WHERE (kind = 'session' AND status = 'ended')"
            " OR (kind = 'subagent' AND status IN ('done', 'ended'))")]
        for key in ids:
            self.db.execute("DELETE FROM events WHERE agent_id = ?", (key,))
            self.db.execute("DELETE FROM agents WHERE id = ?", (key,))
        self.db.commit()

    # ---- reading ----------------------------------------------------------
    @staticmethod
    def display_status(row, now: float) -> str:
        if row["status"] == "working" and now - row["last_event_at"] > STALE_AFTER_S:
            return "stale"
        return row["status"]

    def _as_dict(self, row, now: float) -> dict:
        d = dict(row)
        d["display"] = self.display_status(row, now)
        return d

    def list_agents(self, now: float | None = None) -> list:
        """Sessions (active first), each with 'children' (subagents)."""
        now = time.time() if now is None else now
        rows = [self._as_dict(r, now) for r in self.db.execute("SELECT * FROM agents")]
        children = {}
        for r in sorted((r for r in rows if r["kind"] == "subagent"), key=lambda r: r["started_at"]):
            children.setdefault(r["parent_id"], []).append(r)
        sessions = [r for r in rows if r["kind"] == "session"]
        order = {"waiting": 0, "working": 1, "stale": 2, "idle": 3, "done": 3, "ended": 4}
        sessions.sort(key=lambda r: (order.get(r["display"], 5), -r["last_event_at"]))
        for r in sessions:
            r["children"] = children.get(r["id"], [])
        return sessions

    def active_count(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return sum(1 for r in self.db.execute("SELECT * FROM agents")
                   if r["status"] in ACTIVE and self.display_status(r, now) != "stale")

    def events(self, agent_id: str, limit: int = 200) -> list:
        rows = self.db.execute(
            "SELECT ts, type, tool, summary FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
            (agent_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def all_agents(self) -> list:
        return [dict(r) for r in self.db.execute("SELECT * FROM agents")]

    def all_events(self, limit: int = 20000) -> list:
        """Events in chronological order (for the graph and the replay)."""
        rows = self.db.execute(
            "SELECT ts, agent_id, type, tool, summary FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get(self, agent_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return self._as_dict(row, time.time()) if row else None
