"""
Claude Code hook for NINA. Standard library only: it runs on every tool call and has to be instant.

Reads the hook event JSON from stdin and forwards one line to NINA over a Unix socket (a named pipe on
Windows). It never gets in Claude Code's way: any error is swallowed, the exit code is always 0 and nothing is ever written to stdout
(on some events stdout becomes model context).

Run it as `python -m nina.hook` (this is what `nina --install-hooks` registers) or via the `nina-hook` command.
"""
import json
import sys

from nina.sockpath import send_line, socket_paths

TIMEOUT_S = 0.3


def _clip(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _tool_detail(tool_input) -> str:
    """Short summary of what the tool is about to do (command, file, pattern...)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt"):
        if tool_input.get(key):
            return _clip(tool_input[key], 160)
    return ""


def forward() -> None:
    # pythonw.exe can start with no stdin object; fall back to the inherited handle.
    raw = sys.stdin.read() if sys.stdin is not None else open(0, encoding="utf-8", errors="replace").read()
    data = json.loads(raw or "{}")
    event = {
        "ev": data.get("hook_event_name"),
        "sid": data.get("session_id"),
        "tool": data.get("tool_name"),
        "ntype": data.get("notification_type"),
        "msg": _clip(data.get("message"), 200),
        # Context for the agents panel
        "aid": data.get("agent_id"),
        "atype": data.get("agent_type"),
        "cwd": data.get("cwd"),
        "tp": data.get("transcript_path"),
        "detail": _tool_detail(data.get("tool_input")),
        "prompt": _clip(data.get("prompt"), 160),
        "last": _clip(data.get("last_assistant_message"), 300),
        "reason": _clip(data.get("reason"), 60),
    }
    if not event["ev"]:
        return
    event = {k: v for k, v in event.items() if v not in (None, "")}
    line = "hook " + json.dumps(event, ensure_ascii=False) + "\n"
    for path in socket_paths():
        try:
            send_line(path, line.encode(), TIMEOUT_S)
            return
        except OSError:
            continue


def run() -> None:
    """Entry point: never raises, never prints, always exits 0."""
    try:
        forward()
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    run()
