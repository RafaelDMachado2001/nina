"""History of the conversations with NINA: what was said, and what is needed to pick one up again.

Two small files per conversation in `<state>/nina/history/`, both written atomically and readable only by you:

- `<id>.meta.json`: title, account, folder, Claude session id, dates, cost, message count and a short preview. This is
  all the history list ever reads, so it stays fast and light however many conversations pile up.
- `<id>.msgs.gz`: the messages as they were shown, gzip-compressed. Read only when you resume that conversation (or
  reopen the tab). The assistant text was already redacted by the agent runner.

A session only resumes with the same account and folder, so those travel with it. Nothing is deleted by itself unless
you set `history_keep_days` in the config; otherwise you delete conversations from the history list.
"""
import gzip
import json
import os
import random
import re
import time
from pathlib import Path

MAX_ENTRIES = 3000
MAX_TEXT = 20000
MAX_LISTED = 1000
PREVIEW = 160
ID_RE = re.compile(r"[0-9a-z]{1,16}")


def history_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "history"


_last_id = 0


def new_id() -> str:
    """Time-ordered and unique: two conversations created in the same millisecond still get different ids."""
    global _last_id
    value = max(int(time.time() * 1000) * 256 + random.randrange(256), _last_id + 1)
    _last_id = value
    return format(value, "x")


def clean(raw) -> dict | None:
    """A valid record from whatever was read, or None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not ID_RE.fullmatch(raw["id"]):
        return None
    entries = []
    for e in raw.get("entries") or []:
        if isinstance(e, (list, tuple)) and len(e) == 2:
            entries.append([str(e[0]), str(e[1])[:MAX_TEXT]])
    return {
        "id": raw["id"], "title": str(raw.get("title") or "")[:80], "account": str(raw.get("account") or ""),
        "cwd": str(raw.get("cwd") or ""), "session": str(raw.get("session") or ""),
        "created": float(raw.get("created") or 0), "updated": float(raw.get("updated") or 0),
        "cost": float(raw.get("cost") or 0), "tokens": int(raw.get("tokens") or 0),
        "entries": entries[-MAX_ENTRIES:],
    }


def _write(target: Path, data: bytes) -> None:
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    tmp.replace(target)


def _meta_of(rec: dict) -> dict:
    first = next((t for k, t in rec["entries"] if k == "user"), "")
    return {"id": rec["id"], "title": rec["title"], "account": rec["account"], "cwd": rec["cwd"],
            "session": rec["session"], "created": rec["created"], "updated": rec["updated"], "cost": rec["cost"],
            "tokens": rec["tokens"], "count": message_count(rec), "preview": " ".join(first.split())[:PREVIEW]}


def save(record: dict, base: Path | None = None) -> None:
    rec = clean(record)
    if rec is None:
        return
    folder = base or history_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        body = json.dumps(rec["entries"], ensure_ascii=False, separators=(",", ":")).encode()
        _write(folder / f"{rec['id']}.msgs.gz", gzip.compress(body, 6))
        _write(folder / f"{rec['id']}.meta.json", json.dumps(_meta_of(rec), ensure_ascii=False).encode())  # last: the list
    except OSError:                                                                                         # shows it only
        pass                                                                                               # when complete


def clean_meta(raw) -> dict | None:
    rec = clean({**raw, "entries": []}) if isinstance(raw, dict) else None
    if rec is None:
        return None
    del rec["entries"]
    rec["count"] = int(raw.get("count") or 0)
    rec["preview"] = str(raw.get("preview") or "")[:PREVIEW]
    return rec


def load_meta(conv: str, base: Path | None = None) -> dict | None:
    if not ID_RE.fullmatch(conv or ""):
        return None
    try:
        return clean_meta(json.loads(((base or history_dir()) / f"{conv}.meta.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def load(conv: str, base: Path | None = None) -> dict | None:
    """The whole conversation, messages included. This is the only call that reads (and decompresses) them."""
    meta = load_meta(conv, base)
    if meta is None:
        return None
    try:
        raw = json.loads(gzip.decompress(((base or history_dir()) / f"{conv}.msgs.gz").read_bytes()))
    except (OSError, ValueError, EOFError):
        raw = []
    rec = clean({**meta, "entries": raw if isinstance(raw, list) else []})
    return rec


def list_meta(base: Path | None = None) -> list:
    """Every saved conversation without its messages, the most recently used first."""
    folder = base or history_dir()
    try:
        files = list(folder.glob("*.meta.json"))
    except OSError:
        return []
    metas = [m for m in (load_meta(p.name[: -len(".meta.json")], folder) for p in files) if m and m["count"]]
    return sorted(metas, key=lambda m: m["updated"], reverse=True)[:MAX_LISTED]


def delete(conv: str, base: Path | None = None) -> None:
    if ID_RE.fullmatch(conv or ""):
        for name in (f"{conv}.meta.json", f"{conv}.msgs.gz"):
            try:
                ((base or history_dir()) / name).unlink(missing_ok=True)
            except OSError:
                pass


def usage(base: Path | None = None) -> tuple[int, int]:
    """(conversations, bytes on disk)."""
    folder = base or history_dir()
    count = size = 0
    try:
        for p in folder.iterdir():
            if p.name.endswith(".meta.json"):
                count += 1
            if p.name.endswith((".meta.json", ".msgs.gz")):
                size += p.stat().st_size
    except OSError:
        pass
    return count, size


def prune(days: int, keep: set | None = None, base: Path | None = None, now: float | None = None) -> int:
    """Delete conversations not used for `days` days (0 or less keeps everything), except those in `keep`."""
    if not days or days <= 0:
        return 0
    limit = (time.time() if now is None else now) - days * 86400
    removed = 0
    for m in list_meta(base):
        if m["updated"] and m["updated"] < limit and m["id"] not in (keep or set()):
            delete(m["id"], base)
            removed += 1
    return removed


def matches(meta: dict, query: str) -> bool:
    """Every word must appear in the title, account, folder or the preview. Message text is not searched: reading
    it would mean opening every conversation."""
    words = (query or "").lower().split()
    if not words:
        return True
    hay = " ".join([meta["title"], meta["account"], meta["cwd"], meta.get("preview", "")]).lower()
    return all(w in hay for w in words)


def message_count(record: dict) -> int:
    return sum(1 for kind, _t in record["entries"] if kind in ("user", "assistant"))
