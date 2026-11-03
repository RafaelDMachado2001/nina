"""
Managed agents: NINA launches and talks to Claude Code agents through the Claude Agent SDK (optional).

Everything runs on its own thread with an asyncio loop; the UI receives events through the callback `emit(agent_id, kind, payload)`
(called from that thread: the receiver must hand over to the UI thread, e.g. a Qt signal).

Safety: every permission request first goes through `decide` (the role's policy: reads pre-approved, writes only in the
role's paths) and, unless denied or allowed outright, becomes a confirmation request to the user. Nothing runs without it.
Outgoing events are length-limited and go through `redact`.
"""
import asyncio
import fnmatch
import os
import shutil
import threading
import uuid
from pathlib import Path

from nina.agents.store import clip, redact
from nina.i18n import _

try:
    import claude_agent_sdk as sdk
except Exception:   # the SDK is optional: without it, managed agents are unavailable
    sdk = None

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PERMISSION_TIMEOUT_S = 600


def available() -> bool:
    return sdk is not None


def find_claude_bin(configured: str = "") -> str | None:
    """Use the claude that is already installed (same version you use in the terminal). None = the binary bundled with the SDK."""
    for candidate in (configured, shutil.which("claude") or "", str(Path.home() / ".local" / "bin" / "claude")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


# ---- permission policy (pure, testable) -------------------------------------------------------
def _target_path(tool_input: dict) -> str:
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")


def decide(tool: str, tool_input: dict, role: dict, cwd: str) -> tuple:
    """('allow'|'deny'|'ask', reason). A write outside the role's paths is denied without even asking."""
    if tool in role.get("tools", []):
        return "allow", ""
    if tool in WRITE_TOOLS:
        raw = _target_path(tool_input)
        if not raw:
            return "deny", _("Missing file path.")
        base = os.path.realpath(cwd)
        full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
        if full != base and not full.startswith(base + os.sep):
            return "deny", _("Write outside the agent's folder: {path}", path=raw)
        rel = os.path.relpath(full, base)
        if not any(fnmatch.fnmatch(rel, pattern) for pattern in role.get("writes", [])):
            allowed = ", ".join(role.get("writes", [])) or _("none")
            return "deny", _("This role cannot write to {path} (allowed: {allowed}).", path=rel, allowed=allowed)
    return "ask", ""


def describe(tool: str, tool_input: dict, max_lines: int = 24) -> str:
    """Short text of what the tool is about to do, for the confirmation dialog."""
    if tool in ("Write",):
        content = str(tool_input.get("content", "")).splitlines()
        head = "\n".join(content[:max_lines]) + (f"\n… (+{len(content) - max_lines} lines)" if len(content) > max_lines else "")
        text = _("Create/overwrite {path}:", path=_target_path(tool_input)) + f"\n\n{head}"
    elif tool in ("Edit", "MultiEdit"):
        text = (_("Edit {path}:", path=_target_path(tool_input)) + f"\n\n- {str(tool_input.get('old_string', ''))[:600]}\n"
                f"+ {str(tool_input.get('new_string', ''))[:600]}")
    elif tool == "Bash":
        text = _("Run in the shell:") + f"\n\n{tool_input.get('command', '')}"
    else:
        text = "\n".join(f"{k}: {str(v)[:300]}" for k, v in list(tool_input.items())[:6]) or _("(no parameters)")
    return redact(text)[:3000]


def tool_summary(tool_input: dict) -> str:
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt"):
        if tool_input.get(key):
            return clip(tool_input[key], 120)
    return ""


class _Agent:
    def __init__(self, agent_id: str, role: dict, cwd: str, account: str, config_dir: str | None):
        self.id, self.role, self.cwd, self.account, self.config_dir = agent_id, role, cwd, account, config_dir
        self.session_id = None
        self.queue = None            # asyncio.Queue, created inside the loop
        self.client = None
        self.task = None
        self.pending = set()         # ids of open permission requests


class Runner:
    def __init__(self, emit, claude_bin: str | None = None, client_factory=None):
        self.emit = emit
        self.claude_bin = claude_bin
        self.factory = client_factory or (lambda options: sdk.ClaudeSDKClient(options))
        self.agents = {}
        self.pending = {}            # req_id -> asyncio.Future
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._main, name="nina-managed", daemon=True)
        self.thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- API (called from the UI thread) ---------------------------------------------------------
    def start(self, role: dict, cwd: str, prompt: str, account: str = "", config_dir: str | None = None) -> str:
        agent = _Agent(uuid.uuid4().hex[:12], role, str(cwd), account, config_dir)
        self.agents[agent.id] = agent
        self._submit(self._launch(agent, prompt))
        return agent.id

    def send(self, agent_id: str, text: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is not None and text.strip():
            self.loop.call_soon_threadsafe(self._enqueue, agent, text)

    def interrupt(self, agent_id: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is not None:
            self._submit(self._interrupt(agent))

    def stop(self, agent_id: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is not None:
            self.loop.call_soon_threadsafe(self._stop, agent)

    def resolve(self, req_id: str, allow: bool, message: str = "") -> None:
        self.loop.call_soon_threadsafe(self._resolve, req_id, allow, message)

    def shutdown(self) -> None:
        """Stop every agent and wait (up to 3 s) for them to exit before stopping the loop."""
        for agent_id in list(self.agents):
            self.stop(agent_id)

        async def close() -> None:
            tasks = [a.task for a in self.agents.values() if a.task is not None and not a.task.done()]
            if tasks:
                await asyncio.wait(tasks, timeout=3)
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(close(), self.loop)
        self.thread.join(timeout=5)
        if not self.loop.is_running():
            self.loop.close()

    # ---- asyncio loop -------------------------------------------------------------------------
    def _enqueue(self, agent: _Agent, text) -> None:
        if agent.queue is not None:
            agent.queue.put_nowait(text)

    def _stop(self, agent: _Agent) -> None:
        for req in list(agent.pending):
            self._resolve(req, False, _("Agent closed."))
        if agent.queue is not None:
            agent.queue.put_nowait(None)
        if agent.task is not None and not agent.task.done():
            self.loop.call_later(5, agent.task.cancel)    # force-stop if it does not exit by itself

    def _resolve(self, req_id: str, allow: bool, message: str) -> None:
        fut = self.pending.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result((allow, message))

    async def _interrupt(self, agent: _Agent) -> None:
        if agent.client is not None:
            try:
                await agent.client.interrupt()
            except Exception as e:
                self.emit(agent.id, "error", _("Could not interrupt: {error}", error=e))

    def _options(self, agent: _Agent):
        role = agent.role
        env = {}
        if agent.config_dir:
            env = {"CLAUDE_CONFIG_DIR": agent.config_dir, "CLAUDE_ACCOUNT": agent.account}
        kwargs = dict(
            system_prompt={"type": "preset", "preset": "claude_code", "append": role["prompt"]},
            allowed_tools=list(role.get("tools", [])),
            permission_mode=role.get("permission_mode") or "default",
            cwd=agent.cwd, env=env,
            can_use_tool=lambda tool, tool_input, ctx: self._can_use_tool(agent, tool, tool_input),
            stderr=lambda line: self.emit(agent.id, "stderr", clip(line, 300)),
        )
        if role.get("model"):
            kwargs["model"] = role["model"]
        if self.claude_bin:
            kwargs["cli_path"] = self.claude_bin
        if not role.get("writes"):
            kwargs["disallowed_tools"] = sorted(WRITE_TOOLS)     # read-only role: it never even gets to ask
        return sdk.ClaudeAgentOptions(**kwargs)

    async def _launch(self, agent: _Agent, prompt: str) -> None:
        agent.queue = asyncio.Queue()
        agent.task = asyncio.current_task()
        try:
            async with self.factory(self._options(agent)) as client:
                agent.client = client
                text = prompt
                while text is not None:
                    self.emit(agent.id, "status", "working")
                    await client.query(text)
                    async for msg in client.receive_response():
                        self._handle(agent, msg)
                    self.emit(agent.id, "status", "idle")
                    text = await agent.queue.get()      # None = close
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.emit(agent.id, "error", clip(f"{type(e).__name__}: {e}", 300))
        finally:
            agent.client = None
            self.emit(agent.id, "status", "closed")

    def _handle(self, agent: _Agent, msg) -> None:
        kind = type(msg).__name__
        if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
            sid = (getattr(msg, "data", None) or {}).get("session_id")
            if sid:
                agent.session_id = sid
                self.emit(agent.id, "session", sid)
        elif kind == "AssistantMessage":
            for block in msg.content:
                block_kind = type(block).__name__
                if block_kind == "TextBlock" and block.text.strip():
                    self.emit(agent.id, "text", redact(block.text))
                elif block_kind == "ToolUseBlock":
                    self.emit(agent.id, "tool", {"name": block.name, "summary": tool_summary(block.input or {})})
        elif kind == "ResultMessage":
            if getattr(msg, "session_id", None) and not agent.session_id:
                agent.session_id = msg.session_id
                self.emit(agent.id, "session", msg.session_id)
            self.emit(agent.id, "result", {
                "cost": getattr(msg, "total_cost_usd", None), "turns": getattr(msg, "num_turns", None),
                "error": bool(getattr(msg, "is_error", False)), "reason": getattr(msg, "terminal_reason", None),
                "errors": list(getattr(msg, "errors", None) or [])})

    async def _can_use_tool(self, agent: _Agent, tool: str, tool_input: dict):
        verdict, why = decide(tool, tool_input, agent.role, agent.cwd)
        if verdict == "allow":
            return sdk.PermissionResultAllow()
        if verdict == "deny":
            self.emit(agent.id, "denied", {"tool": tool, "why": why})
            return sdk.PermissionResultDeny(message=why)
        req = uuid.uuid4().hex
        fut = self.loop.create_future()
        self.pending[req] = fut
        agent.pending.add(req)
        self.emit(agent.id, "permission", {"req": req, "tool": tool, "detail": describe(tool, tool_input)})
        try:
            allow, message = await asyncio.wait_for(fut, PERMISSION_TIMEOUT_S)
        except asyncio.TimeoutError:
            allow, message = False, _("No answer from the user in time.")
        finally:
            self.pending.pop(req, None)
            agent.pending.discard(req)
        if allow:
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=message or _("Denied by the user."))
