"""
Managed agents: NINA launches and talks to Claude Code agents through the Claude Agent SDK (optional).

Everything runs on its own thread with an asyncio loop; the UI receives events through the callback `emit(agent_id, kind, payload)`
(called from that thread: the receiver must hand over to the UI thread, e.g. a Qt signal).

Safety: every permission request first goes through `decide` (the role's policy: reads pre-approved, writes only in the
role's paths) and, unless denied or allowed outright, becomes a confirmation request to the user. Nothing runs without it.
Outgoing events are length-limited and go through `redact`.
"""
import asyncio
import os
import shutil
import threading
import uuid
from pathlib import Path

from nina.agents.permissions import WRITE_TOOLS, Gate, decide, describe
from nina.agents.store import clip, redact
from nina.i18n import _

__all__ = ["Runner", "available", "can_run", "decide", "describe", "find_claude_bin", "has_content", "query_arg", "tool_summary"]

try:
    import claude_agent_sdk as sdk
except Exception:   # the SDK is optional: without it, managed agents are unavailable
    sdk = None


def has_content(message) -> bool:
    """A message is a string, or {"text": str, "images": [{"media_type", "data" (base64)}]}."""
    if isinstance(message, dict):
        return bool(str(message.get("text", "")).strip() or message.get("images"))
    return bool(message and str(message).strip())


def query_arg(message):
    """What `client.query` takes: plain text, or a streamed user message with image blocks."""
    if not isinstance(message, dict):
        return message
    images = message.get("images") or []
    text = str(message.get("text", ""))
    if not images:
        return text

    async def stream():
        content = [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
                   for i in images]
        if text.strip():
            content.append({"type": "text", "text": text})
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None,
               "session_id": "default"}
    return stream()


def available() -> bool:
    return sdk is not None


def can_run(connection: dict | None) -> bool:
    """Whether an agent on this connection can start here (Claude needs the optional SDK)."""
    if connection is not None and connection.get("kind") == "openai":
        return True
    return sdk is not None


def find_claude_bin(configured: str = "") -> str | None:
    """Use the claude that is already installed (same version you use in the terminal). None = the binary bundled with the SDK."""
    for candidate in (configured, shutil.which("claude") or "", str(Path.home() / ".local" / "bin" / "claude")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def tool_summary(tool_input: dict) -> str:
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt"):
        if tool_input.get(key):
            return clip(tool_input[key], 120)
    return ""


class _Agent:
    def __init__(self, agent_id: str, role: dict, cwd: str, account: str, config_dir: str | None,
                 connection: dict | None = None, model: str = ""):
        self.id, self.role, self.cwd, self.account, self.config_dir = agent_id, role, cwd, account, config_dir
        self.connection, self.model = connection, model
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
        self.auto = None             # an autoapprove.AutoMode: read-only Bash runs without asking while it is on
        self.deny = []               # the user's NINA deny list (rules), checked first for every agent
        self.max_steps = 40          # tool calls per answer for agents on OpenAI-compatible connections
        self.loop = asyncio.new_event_loop()
        self.gate = Gate(self.loop, self._emit, lambda: self.auto, lambda: self.deny)
        self.thread = threading.Thread(target=self._main, name="nina-managed", daemon=True)
        self.thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- API (called from the UI thread) ---------------------------------------------------------
    def start(self, role: dict, cwd: str, prompt: str | None, account: str = "", config_dir: str | None = None,
              resume: str | None = None, connection: dict | None = None, model: str = "") -> str:
        """Start an agent. With `resume` (a session id) the conversation continues where it stopped; `prompt` may
        then be None to wait for the user's next message. `connection` (see nina.connections) picks the AI: None or a
        "claude" connection runs Claude through the SDK, an "openai" one runs NINA's own tool loop."""
        agent = _Agent(uuid.uuid4().hex[:12], role, str(cwd), account, config_dir, connection, model)
        agent.session_id = resume
        self.agents[agent.id] = agent
        self._submit(self._launch(agent, prompt))
        return agent.id

    def send(self, agent_id: str, text: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is not None and has_content(text):
            self.loop.call_soon_threadsafe(self._enqueue, agent, text)

    def interrupt(self, agent_id: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is not None:
            self._submit(self._interrupt(agent))

    def stop(self, agent_id: str) -> None:
        agent = self.agents.get(agent_id)
        if agent is not None:
            self.loop.call_soon_threadsafe(self._stop, agent)

    def resolve(self, req_id: str, allow: bool, message: str = "", always: bool = False) -> None:
        self.loop.call_soon_threadsafe(self._resolve, req_id, allow, message, always)

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
            self._resolve(req, False, _("Agent closed."), False)
        if agent.queue is not None:
            agent.queue.put_nowait(None)
        if agent.task is not None and not agent.task.done():
            self.loop.call_later(5, agent.task.cancel)    # force-stop if it does not exit by itself

    def _resolve(self, req_id: str, allow: bool, message: str, always: bool = False) -> None:
        self.gate.resolve(req_id, allow, message, always)

    def _emit(self, agent_id: str, kind: str, payload) -> None:
        self.emit(agent_id, kind, payload)

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
        if agent.session_id:
            kwargs["resume"] = agent.session_id
        if agent.model or role.get("model"):
            kwargs["model"] = agent.model or role["model"]
        if self.claude_bin:
            kwargs["cli_path"] = self.claude_bin
        if not role.get("writes"):
            kwargs["disallowed_tools"] = sorted(WRITE_TOOLS)     # read-only role: it never even gets to ask
        return sdk.ClaudeAgentOptions(**kwargs)

    async def _launch(self, agent: _Agent, prompt: str) -> None:
        if agent.connection is not None and agent.connection.get("kind") == "openai":
            from nina.agents.openai_agent import OpenAIAgent
            await OpenAIAgent(agent, agent.connection, agent.model, self.gate, self._emit, self.max_steps).run(prompt)
            return
        agent.queue = asyncio.Queue()
        agent.task = asyncio.current_task()
        try:
            async with self.factory(self._options(agent)) as client:
                agent.client = client
                text = prompt
                while True:
                    if text:
                        self.emit(agent.id, "status", "working")
                        await client.query(query_arg(text))
                        async for msg in client.receive_response():
                            self._handle(agent, msg)
                    await self._report_context(agent, client)
                    self.emit(agent.id, "status", "idle")
                    text = await agent.queue.get()      # None = close
                    if text is None:
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.emit(agent.id, "error", clip(f"{type(e).__name__}: {e}", 300))
        finally:
            agent.client = None
            self.emit(agent.id, "status", "closed")

    async def _report_context(self, agent: _Agent, client) -> None:
        """How full the context window is after a turn (what /context shows). Best effort: never breaks the chat."""
        try:
            u = await client.get_context_usage()
            self.emit(agent.id, "context", {"tokens": int(u["totalTokens"]), "max": int(u["maxTokens"]),
                                            "model": str(u.get("model") or "")})
        except Exception:
            pass

    def _handle(self, agent: _Agent, msg) -> None:
        kind = type(msg).__name__
        if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
            sid = (getattr(msg, "data", None) or {}).get("session_id")
            if sid:
                agent.session_id = sid
                self.emit(agent.id, "session", sid)
            if (getattr(msg, "data", None) or {}).get("model"):
                self.emit(agent.id, "model", msg.data["model"])
        elif kind == "RateLimitEvent":
            info = msg.rate_limit_info
            self.emit(agent.id, "ratelimit", {
                "status": info.status, "type": info.rate_limit_type, "utilization": info.utilization,
                "resets_at": info.resets_at, "windows": (info.raw or {}).get("unifiedWindows") or {}})
        elif kind == "AssistantMessage":
            usage = getattr(msg, "usage", None) or {}
            new = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
            if new:
                self.emit(agent.id, "usage", new)
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
        allowed, message = await self.gate.check(agent.id, agent.role, agent.cwd, tool, tool_input, agent.pending)
        if allowed:
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=message)
