"""The permission gate every agent goes through, whatever AI it runs on. Standard library only.

Order: the user's NINA deny list (`"permissions": {"deny": [...]}` in config.json) -> the role's policy (`tools` are
pre-approved; writes outside the role's paths are denied without asking) -> auto mode for shell commands -> ask the
user (Allow / Deny), 10 minutes at most.

Rules use Claude Code's syntax: `Tool`, `Tool(exact)` or `Tool(prefix:*)`. For Bash the parentheses match the
command; for other tools they are a glob on the file path (relative to the agent's folder, or absolute; `~` works) or
the URL.

Rules fail safe. A pre-approval (`allow`) of a Bash rule only covers a single plain command: anything chained (`;`,
`&&`, `|`, a newline), substituted (`$(...)`, backticks) or redirected asks. A deny rule matches any part of a command,
including what runs inside a substitution and after `sudo`. Paths are normalised (`..`, `./`, `~`, absolute or not)
before they are compared.
"""
import asyncio
import fnmatch
import os
import re
import shlex
import uuid

from nina.autoapprove import _split, _tokens
from nina.agents.store import redact
from nina.i18n import _

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PERMISSION_TIMEOUT_S = 600
_RULE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*(?:\((.*)\))?\s*$", re.S)


def target_path(tool_input: dict) -> str:
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")


WRAPPERS = {"sudo", "doas", "env", "nohup", "nice", "time", "command", "builtin", "exec"}


def _command_parts(command: str, depth: int = 0) -> tuple:
    """(token lists of every command in `command`, including substitutions, and whether it is one plain command)."""
    segments, inner, flags = _split(command)
    parts, plain = [], flags["ok"] and not flags["redirect"] and not flags["heredoc"] and not inner and len(segments) == 1
    for segment in segments:
        tokens = _tokens(segment)
        if tokens is None:
            plain = False
            continue
        while tokens and tokens[0] in WRAPPERS:
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):      # sudo -u x ...: drop the wrapper's options
                tokens = tokens[1:]
        parts.append(tokens)
    if depth < 3:
        for text in inner:
            parts += _command_parts(text, depth + 1)[0]
    return parts, plain and flags["ok"]


def _bash_matches(spec: str, command: str, deny: bool) -> bool:
    prefix_mode = spec.endswith(":*")
    try:
        wanted = shlex.split(spec[:-2] if prefix_mode else spec)
    except ValueError:
        return False
    if not wanted:
        return False
    parts, plain = _command_parts(command)

    def hit(tokens):
        return tokens[:len(wanted)] == wanted if prefix_mode else tokens == wanted
    if deny:
        if any(hit(t) for t in parts):
            return True
        # unparseable text: be conservative and look for the words anywhere
        return not plain and " ".join(wanted) in " ".join(command.split())
    return plain and len(parts) == 1 and hit(parts[0])


def _path_forms(raw: str, cwd: str) -> list:
    """The absolute path and (inside the folder) the folder-relative path, both with "/" and `..` resolved."""
    if not raw:
        return []
    expanded = os.path.expanduser(raw)
    full = os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(cwd or os.getcwd(), expanded))
    forms = [full.replace("\\", "/")]
    if cwd:
        base = os.path.normpath(cwd)
        if full == base or full.startswith(base.rstrip(os.sep) + os.sep):
            forms.append(os.path.relpath(full, base).replace("\\", "/"))
    return forms


def _glob_matches(pattern: str, forms: list) -> bool:
    pattern = os.path.expanduser(pattern.strip()).replace("\\", "/")
    patterns = [pattern] + ([pattern[3:]] if pattern.startswith("**/") else [])   # **/x also matches x at the top
    return any(fnmatch.fnmatch(form, p) for form in forms for p in patterns)


def rule_matches(rule: str, tool: str, tool_input: dict, cwd: str = "", deny: bool = False) -> bool:
    """Whether a rule covers this call. `deny` rules match any part of a command; allow rules fail safe."""
    m = _RULE.match(str(rule or ""))
    if not m or m.group(1) != tool:
        return False
    spec = m.group(2)
    if spec is None:
        return True
    spec = spec.strip()
    if tool == "Bash":
        return _bash_matches(spec, str(tool_input.get("command", "")), deny)
    path = target_path(tool_input)
    if path:
        return _glob_matches(spec, _path_forms(path, cwd))
    url = str(tool_input.get("url", "")).strip()
    return bool(url) and fnmatch.fnmatch(url, spec)


def decide(tool: str, tool_input: dict, role: dict, cwd: str, deny=()) -> tuple:
    """('allow'|'deny'|'ask', reason). A write outside the role's paths is denied without even asking."""
    for rule in deny or ():
        if rule_matches(rule, tool, tool_input, cwd, deny=True):
            return "deny", _("Blocked by your NINA permissions: {rule}", rule=rule)
    preapproved = any(rule_matches(r, tool, tool_input, cwd) for r in role.get("tools", []))
    if tool in WRITE_TOOLS:     # a pre-approval never lifts the folder and the role's paths
        raw = target_path(tool_input)
        if not raw:
            return "deny", _("Missing file path.")
        base = os.path.realpath(cwd)
        full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
        if full != base and not full.startswith(base + os.sep):
            return "deny", _("Write outside the agent's folder: {path}", path=raw)
        rel = os.path.relpath(full, base)
        if not any(fnmatch.fnmatch(rel.replace(os.sep, "/"), pattern) for pattern in role.get("writes", [])):
            allowed = ", ".join(role.get("writes", [])) or _("none")
            return "deny", _("This role cannot write to {path} (allowed: {allowed}).", path=rel, allowed=allowed)
    return ("allow", "") if preapproved else ("ask", "")


def describe(tool: str, tool_input: dict, max_lines: int = 24) -> str:
    """Short text of what the tool is about to do, for the confirmation dialog."""
    if tool in ("Write",):
        content = str(tool_input.get("content", "")).splitlines()
        head = "\n".join(content[:max_lines]) + (f"\n… (+{len(content) - max_lines} lines)" if len(content) > max_lines else "")
        text = _("Create/overwrite {path}:", path=target_path(tool_input)) + f"\n\n{head}"
    elif tool in ("Edit", "MultiEdit"):
        text = (_("Edit {path}:", path=target_path(tool_input)) + f"\n\n- {str(tool_input.get('old_string', ''))[:600]}\n"
                f"+ {str(tool_input.get('new_string', ''))[:600]}")
    elif tool == "Bash":
        text = _("Run in the shell:") + f"\n\n{tool_input.get('command', '')}"
    elif tool == "Delegate":
        text = _("Hand this task to {role}:", role=tool_input.get("role", "")) + f"\n\n{tool_input.get('task', '')}"
    elif tool == "WebFetch":
        text = _("Fetch this web page:") + f"\n\n{tool_input.get('url', '')}"
    else:
        text = "\n".join(f"{k}: {str(v)[:300]}" for k, v in list(tool_input.items())[:6]) or _("(no parameters)")
    return redact(text)[:3000]


class Gate:
    """Lives on the agents' asyncio loop. `emit(agent_id, kind, payload)` reports `denied` and `permission` events;
    `resolve()` (called on the loop) answers a permission request."""

    def __init__(self, loop, emit, auto_getter, deny_getter):
        self.loop, self.emit = loop, emit
        self.auto_getter, self.deny_getter = auto_getter, deny_getter
        self.pending = {}            # req_id -> Future[(allow, message, always)]
        self.remember = {}           # req_id -> the auto mode steps "Allow always" would save

    async def check(self, agent_id: str, role: dict, cwd: str, tool: str, tool_input: dict, pending: set) -> tuple:
        """(allowed, message). `pending` is the agent's own set of open request ids (so closing it can deny them)."""
        if tool == "mcp__nina__delegate":        # Claude agents call NINA's Delegate through an SDK MCP server
            tool = "Delegate"
        verdict, why = decide(tool, tool_input, role, cwd, self.deny_getter() or ())
        if verdict == "allow":
            return True, ""
        if verdict == "deny":
            self.emit(agent_id, "denied", {"tool": tool, "why": why})
            return False, why
        auto = self.auto_getter()
        steps = []
        if tool == "Bash" and auto is not None and auto.enabled():
            steps = auto.pending(str(tool_input.get("command", "")))
            if not steps:
                return True, ""          # auto mode: it only reads, or you already allowed it always
        req = uuid.uuid4().hex
        fut = self.loop.create_future()
        self.pending[req] = fut
        pending.add(req)
        offer = steps if steps and not any(s.hard for s in steps) else []
        if offer:
            self.remember[req] = offer
        self.emit(agent_id, "permission", {"req": req, "tool": tool, "detail": describe(tool, tool_input),
                                           "remember": [s.label for s in offer]})
        try:
            allow, message, always = await asyncio.wait_for(fut, PERMISSION_TIMEOUT_S)
        except asyncio.TimeoutError:
            allow, message, always = False, _("No answer from the user in time."), False
        finally:
            self.pending.pop(req, None)
            pending.discard(req)
        remembered = self.remember.pop(req, [])
        if allow:
            if always and remembered and auto is not None:
                auto.rules.add(remembered)
            return True, ""
        return False, message or _("Denied by the user.")

    def resolve(self, req_id: str, allow: bool, message: str = "", always: bool = False) -> None:
        fut = self.pending.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result((allow, message, always))
