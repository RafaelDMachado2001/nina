"""
Tolerant reader for Claude Code transcripts (JSONL under ~/.claude/projects/...).

The format is NOT documented and changes between versions, so everything here is best effort: unknown
fields are ignored and any error yields an empty list. Hooks remain the source of truth for state;
the transcript only enriches the screen with what the agent said and did.
Thoughts (`thinking`) are deliberately ignored.
"""
import json
from pathlib import Path

from nina.agents.store import clip

MAX_BYTES = 262144   # reads only the end of the file


def _tool_summary(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(key):
            return str(inp[key])
    return ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def tail_messages(path, limit: int = 30) -> list:
    """Latest messages as [{ts, role, text}], role in user|assistant|tool|result."""
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - MAX_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if size > MAX_BYTES:
            lines = lines[1:]   # the first line is probably cut off
    except (OSError, ValueError):
        return []
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isMeta") or d.get("type") not in ("user", "assistant"):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        content, ts = msg.get("content"), d.get("timestamp")
        if isinstance(content, str):
            if content.strip():
                out.append({"ts": ts, "role": d["type"], "text": clip(content, 400)})
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and str(block.get("text", "")).strip():
                out.append({"ts": ts, "role": d["type"], "text": clip(block["text"], 400)})
            elif kind == "tool_use":
                out.append({"ts": ts, "role": "tool",
                            "text": clip(f"{block.get('name', '?')}: {_tool_summary(block.get('input'))}", 200)})
            elif kind == "tool_result":
                text = _result_text(block.get("content"))
                if text.strip():
                    out.append({"ts": ts, "role": "result", "text": clip(text, 200)})
    return out[-limit:]


def subagent_files(session_transcript: str, agent_id: str) -> tuple:
    """(transcript, meta) of a subagent, by the convention <session>/subagents/agent-<id>.jsonl."""
    base = Path(session_transcript).with_suffix("") / "subagents" / f"agent-{agent_id}"
    return base.with_suffix(".jsonl"), base.with_suffix(".meta.json")


def read_meta(path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def find_title(path) -> str:
    """Automatic session title (`ai-title` record), searched at the end of the transcript."""
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - MAX_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    for line in reversed(lines):
        if '"ai-title"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "ai-title" and d.get("aiTitle"):
            return clip(d["aiTitle"], 90)
    return ""
