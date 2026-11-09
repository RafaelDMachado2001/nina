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

__all__ = ["Runner", "available", "can_run", "display_tool", "decide", "describe", "find_claude_bin", "has_content", "query_arg", "tool_summary"]

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


def display_tool(name: str) -> str:
    """The name shown in the chat (Claude sees NINA's Delegate as an MCP tool)."""
    return "Delegate" if name == "mcp__nina__delegate" else name


def tool_summary(tool_input: dict) -> str:
    if tool_input.get("role") and tool_input.get("task"):           # Delegate
        return clip(f"{tool_input['role']}: {tool_input['task']}", 120)
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt"):
        if tool_input.get(key):
            return clip(tool_input[key], 120)
    return ""


class _Agent:
    def __init__(self, agent_id: str, role: dict, cwd: str, account: str, config_dir: str | None,
                 connection: dict | None = None, model: str = ""):
        self.id, self.role, self.cwd, self.account, self.config_dir = agent_id, role, cwd, account, config_dir
        self.connection, self.model = connection, model
        self.depth, self.parent, self.budget = 0, None, None     # delegation: level, delegating agent, shared budget
        self.label = ""                                          # "<role> · <AI>" of a delegated agent
        self.stopped = False                                     # set when a delegated agent is ended: no more calls
        self.gate_role = role                                    # the role the gate checks (+ Delegate when pre-approved)
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
        self.connections = []        # the user's connections: their env-var keys are hidden from shell commands
        self.orchestration = None    # {"settings", "roles", "resolve", "conn_name"}; None = manual (no delegation)
        self.children = {}           # delegated agent id -> {"parent", "texts", "done", "error", "cost"}
        self.loop = asyncio.new_event_loop()
        self.gate = Gate(self.loop, self._emit, lambda: self.auto, lambda: self.deny)
        self.gate.describer = self._describe
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
            if agent.depth == 0:
                self._prepare(agent)            # current settings (e.g. "ask" turned back on) and a fresh budget
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
        child = self.children.get(agent_id)
        if child is None:
            self.emit(agent_id, kind, payload)
        else:
            self._child_event(agent_id, child, kind, payload)

    # ---- delegation ---------------------------------------------------------------------------------
    def _settings(self) -> dict:
        from nina import orchestration
        return (self.orchestration or {}).get("settings") or orchestration.settings({})

    def _conn_name_for(self, agent: _Agent):
        """`conn_name(role_id)` as seen from `agent`: a role without its own binding would run on agent's AI."""
        orch = self.orchestration or {}
        roles, resolve = orch.get("roles") or {}, orch.get("resolve")
        if resolve is None or agent.connection is None:
            return orch.get("conn_name") or str

        def name(role_id: str) -> str:
            try:
                return resolve(roles[role_id], agent.connection)[0].get("name", "")
            except Exception:
                return ""
        return name

    def _describe(self, agent_id: str, tool: str, tool_input: dict) -> str | None:
        """The Delegate question names the role and the AI it would run on (it may cost money)."""
        if tool != "Delegate" or not self.orchestration:
            return None
        role = (self.orchestration.get("roles") or {}).get(str(tool_input.get("role", "")))
        if role is None:
            return None
        agent = self.agents.get(agent_id)
        try:
            ai = (self._conn_name_for(agent) if agent else (self.orchestration.get("conn_name") or str))(role["id"])
        except Exception:
            ai = ""
        head = _("Hand this task to {role} (on {ai}):", role=_(role.get("title", role["id"])), ai=ai or "?")
        return f"{head}\n\n{tool_input.get('task', '')}"

    def targets_for(self, agent: _Agent) -> list:
        """Roles this agent may delegate to (empty in manual mode)."""
        from nina import orchestration
        if not self.orchestration:
            return []
        return orchestration.delegates_for(self._settings(), agent.role.get("id", ""), agent.depth,
                                           self.orchestration.get("roles") or {})

    def _prepare(self, agent: _Agent) -> None:
        """The gate's view of the role (Delegate pre-approved when the user turned asking off) and a fresh budget
        for a top-level agent's new message."""
        if self.targets_for(agent) and not self._settings()["ask"]:
            agent.gate_role = {**agent.role, "tools": list(agent.role.get("tools", [])) + ["Delegate"]}
        else:
            agent.gate_role = agent.role
        if agent.depth == 0:
            agent.budget = {"left": self._settings()["max_delegations"]}

    def _root(self, agent: _Agent) -> _Agent:
        while agent.parent is not None:
            agent = agent.parent
        return agent

    def _parent_hook(self, child: _Agent, ev: str, **fields) -> None:
        root = self._root(child)
        data = {"ev": ev, "sid": root.session_id, "aid": child.id, "cwd": child.cwd, **fields}
        self.emit(root.id, "hook", {k: v for k, v in data.items() if v not in (None, "")})

    def _child_event(self, child_id: str, state: dict, kind: str, payload) -> None:
        child, parent = self.agents.get(child_id), state["parent"]
        if child is None:
            return
        if kind == "text":
            state["texts"].append(str(payload))
        elif kind == "status" and payload in ("idle", "closed"):
            state["done"].set()
        elif kind in ("turn_error", "error"):
            state["error"] = str(payload)
            state["done"].set()
        elif kind == "tool":
            state["texts"] = []                  # what it said before using a tool was narration, not the answer
            self._emit(parent.id, "tool", {"name": f"{child.label} → {payload['name']}", "summary": payload.get("summary", "")})
            self._parent_hook(child, "PreToolUse", tool=payload["name"], detail=payload.get("summary", ""))
        elif kind in ("permission", "denied"):
            self._emit(parent.id, kind, {**payload, "tool": f"{child.label} · {payload.get('tool', '')}"})
        elif kind == "usage":
            self._emit(parent.id, "usage", payload)
        elif kind == "extra_cost" and isinstance(payload, (int, float)):     # what its own children spent
            state["cost"] += float(payload)
        elif kind == "result" and isinstance(payload, dict):
            if isinstance(payload.get("cost"), (int, float)):
                state["cost"] += float(payload["cost"])
            if payload.get("error") or payload.get("errors"):
                state["error"] = "; ".join(payload.get("errors") or []) or "the answer ended with an error"

    def delegation_error(self, parent: _Agent, role_id: str, task: str) -> str:
        """Why this delegation cannot happen ("" when it can). Checked before asking the user, so nobody is asked
        to approve something that would be refused anyway."""
        targets = self.targets_for(parent)
        if role_id not in targets:
            return "Error: " + (f"you cannot delegate to {role_id!r}. Available: {', '.join(targets)}" if targets
                                else "delegation is not available here (the orchestration mode does not allow it)")
        if not str(task or "").strip():
            return "Error: the task is empty"
        budget = parent.budget if parent.budget is not None else self._root(parent).budget
        if budget is not None and budget["left"] <= 0:
            limit = self._settings()["max_delegations"]
            return f"Error: the delegation limit for this request was reached ({limit}). Do the rest yourself."
        return ""

    async def delegate(self, parent: _Agent, role_id: str, task: str) -> str:
        """Hand `task` to an agent of `role_id` and return its answer. Never raises (except cancellation): every
        failure is an "Error: ..." text the delegating agent can react to."""
        roles = (self.orchestration or {}).get("roles") or {}
        refused = self.delegation_error(parent, role_id, task)
        if refused:
            return refused
        budget = parent.budget if parent.budget is not None else self._root(parent).budget
        role = roles[role_id]
        try:
            conn, model, account, config_dir = self.orchestration["resolve"](role, parent.connection or {"id": "claude", "kind": "claude", "name": "Claude"})
        except Exception as e:
            return f"Error: no AI for {role_id}: {e}"
        if conn.get("kind") != "openai" and sdk is None:
            return f"Error: {role_id} runs on Claude, which is not installed here"
        if budget is not None:
            budget["left"] -= 1                   # counted once the delegation really starts
        child = _Agent(uuid.uuid4().hex[:12], role, parent.cwd, account, config_dir, conn, model)
        child.depth, child.parent, child.budget = parent.depth + 1, parent, budget
        child.label = f"{_(role.get('title', role_id))} · {conn.get('name', conn.get('id', ''))}"
        state = {"parent": parent, "texts": [], "done": asyncio.Event(), "error": "", "cost": 0.0}
        self.agents[child.id] = child
        self.children[child.id] = state
        self._prepare(child)
        child.budget = budget                    # the whole tree shares one budget
        atype = f"{_(parent.role.get('title', ''))} → {_(role.get('title', role_id))}"
        self._parent_hook(child, "SubagentStart", atype=atype)
        prompt = (f"{task}\n\n(This task was handed to you by another agent, {_(parent.role.get('title', ''))}. Do it "
                  "and reply with the result. You cannot ask the user questions: if something is missing, say so in "
                  "your reply.)")
        run = asyncio.ensure_future(self._launch(child, prompt))
        try:
            await state["done"].wait()
        except asyncio.CancelledError:
            self._end_child(child, run, cancel=True)
            raise
        self._end_child(child, run, cancel=False)
        answer = "\n\n".join(t for t in state["texts"] if t.strip()).strip()
        self._parent_hook(child, "SubagentStop", last=clip(answer or state["error"], 300))
        if state["cost"]:
            self._emit(parent.id, "tool", {"name": child.label, "summary": f"US$ {state['cost']:.4f}"})
            self._emit(parent.id, "extra_cost", state["cost"])        # into the chat's and the day's totals
        if state["error"]:                        # a failure stays a failure, even after some text
            partial = f"\n\nWhat it said before failing:\n{answer[:4000]}" if answer else ""
            return f"Error: {child.label} failed: {state['error']}{partial}"
        if len(answer) > 20000:
            answer = answer[:20000] + "\n… [truncated]"
        return answer or f"Error: {child.label} gave no answer"

    def _delegate_tool(self, agent: _Agent):
        """NINA's Delegate as an in-process MCP tool for a Claude agent (Claude sees it as mcp__nina__delegate)."""
        targets = self.targets_for(agent)

        async def handler(args: dict) -> dict:
            text = await self.delegate(agent, str(args.get("role", "")), str(args.get("task", "")))
            return {"content": [{"type": "text", "text": text}], "is_error": text.startswith("Error:")}

        schema = {"type": "object", "properties": {
            "role": {"type": "string", "enum": targets, "description": "Which role does the task"},
            "task": {"type": "string", "description": "Everything the other agent needs: it does not see this conversation"}},
            "required": ["role", "task"]}
        return sdk.SdkMcpTool(name="delegate", description="Hand a self-contained task to another agent (a role) "
                              "and get its answer back.", input_schema=schema, handler=handler)

    def _end_child(self, child: _Agent, run, cancel: bool) -> None:
        child.stopped = True                     # first: a denied question below must not let it make another call
        for req in list(child.pending):
            self.gate.resolve(req, False, _("Agent closed."), False)
            self.emit(self._root(child).id, "permission_withdrawn", {"req": req})   # the question leaves the screen
        if child.queue is not None:
            child.queue.put_nowait(None)
        if cancel and not run.done():
            run.cancel()
        elif not run.done():
            self.loop.call_later(5, run.cancel)
        run.add_done_callback(lambda _f, cid=child.id: (self.children.pop(cid, None), self.agents.pop(cid, None)))

    async def _interrupt(self, agent: _Agent) -> None:
        if agent.client is not None:
            try:
                await agent.client.interrupt()
            except Exception as e:
                self._emit(agent.id, "error", _("Could not interrupt: {error}", error=e))

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
            stderr=lambda line: self._emit(agent.id, "stderr", clip(line, 300)),
        )
        if agent.session_id:
            kwargs["resume"] = agent.session_id
        if agent.model or role.get("model"):
            kwargs["model"] = agent.model or role["model"]
        if self.claude_bin:
            kwargs["cli_path"] = self.claude_bin
        disallowed = list(self.deny)                           # NINA's deny list, also where can_use_tool is skipped
        if not role.get("writes"):
            disallowed += sorted(WRITE_TOOLS)                  # read-only role: it never even gets to ask
        targets = self.targets_for(agent)
        if targets:
            # It delegates through NINA (limits, prompts, labels), not through Claude's own subagents.
            disallowed += ["Task", "Agent"]
            kwargs["mcp_servers"] = {"nina": sdk.create_sdk_mcp_server("nina", tools=[self._delegate_tool(agent)])}
            from nina import orchestration
            roles = (self.orchestration or {}).get("roles") or {}
            kwargs["system_prompt"] = {**kwargs["system_prompt"], "append": kwargs["system_prompt"]["append"] +
                                       orchestration.delegation_prompt(targets, roles, self._conn_name_for(agent))}
        if disallowed:
            kwargs["disallowed_tools"] = disallowed
        return sdk.ClaudeAgentOptions(**kwargs)

    async def _launch(self, agent: _Agent, prompt: str) -> None:
        if agent.depth == 0:
            self._prepare(agent)
        if agent.connection is not None and agent.connection.get("kind") == "openai":
            from nina.agents.openai_agent import OpenAIAgent
            hide = {c["key"]["name"] for c in self.connections
                    if isinstance(c.get("key"), dict) and c["key"].get("source") == "env" and c["key"].get("name")}
            targets = self.targets_for(agent)
            extra = ""
            if targets:
                from nina import orchestration
                roles = (self.orchestration or {}).get("roles") or {}
                extra = orchestration.delegation_prompt(targets, roles, self._conn_name_for(agent))
            await OpenAIAgent(agent, agent.connection, agent.model, self.gate, self._emit, self.max_steps,
                              hide, delegate=(lambda role, task: self.delegate(agent, role, task)) if targets else None,
                              targets=targets, extra_prompt=extra,
                              precheck=lambda role, task: self.delegation_error(agent, role, task)).run(prompt)
            return
        agent.queue = asyncio.Queue()
        agent.task = asyncio.current_task()
        try:
            async with self.factory(self._options(agent)) as client:
                agent.client = client
                text = prompt
                while True:
                    if text:
                        self._emit(agent.id, "status", "working")
                        await client.query(query_arg(text))
                        async for msg in client.receive_response():
                            self._handle(agent, msg)
                    await self._report_context(agent, client)
                    self._emit(agent.id, "status", "idle")
                    text = await agent.queue.get()      # None = close
                    if text is None:
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._emit(agent.id, "error", clip(f"{type(e).__name__}: {e}", 300))
        finally:
            agent.client = None
            self._emit(agent.id, "status", "closed")

    async def _report_context(self, agent: _Agent, client) -> None:
        """How full the context window is after a turn (what /context shows). Best effort: never breaks the chat."""
        try:
            u = await client.get_context_usage()
            self._emit(agent.id, "context", {"tokens": int(u["totalTokens"]), "max": int(u["maxTokens"]),
                                            "model": str(u.get("model") or "")})
        except Exception:
            pass

    def _handle(self, agent: _Agent, msg) -> None:
        kind = type(msg).__name__
        if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
            sid = (getattr(msg, "data", None) or {}).get("session_id")
            if sid:
                agent.session_id = sid
                self._emit(agent.id, "session", sid)
            if (getattr(msg, "data", None) or {}).get("model"):
                self._emit(agent.id, "model", msg.data["model"])
        elif kind == "RateLimitEvent":
            info = msg.rate_limit_info
            self._emit(agent.id, "ratelimit", {
                "status": info.status, "type": info.rate_limit_type, "utilization": info.utilization,
                "resets_at": info.resets_at, "windows": (info.raw or {}).get("unifiedWindows") or {}})
        elif kind == "AssistantMessage":
            usage = getattr(msg, "usage", None) or {}
            new = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
            if new:
                self._emit(agent.id, "usage", new)
            for block in msg.content:
                block_kind = type(block).__name__
                if block_kind == "TextBlock" and block.text.strip():
                    self._emit(agent.id, "text", redact(block.text))
                elif block_kind == "ToolUseBlock":
                    self._emit(agent.id, "tool", {"name": display_tool(block.name), "summary": tool_summary(block.input or {})})
        elif kind == "ResultMessage":
            if getattr(msg, "session_id", None) and not agent.session_id:
                agent.session_id = msg.session_id
                self._emit(agent.id, "session", msg.session_id)
            self._emit(agent.id, "result", {
                "cost": getattr(msg, "total_cost_usd", None), "turns": getattr(msg, "num_turns", None),
                "error": bool(getattr(msg, "is_error", False)), "reason": getattr(msg, "terminal_reason", None),
                "errors": list(getattr(msg, "errors", None) or [])})

    async def _can_use_tool(self, agent: _Agent, tool: str, tool_input: dict):
        if tool == "mcp__nina__delegate":
            refused = self.delegation_error(agent, str(tool_input.get("role", "")), str(tool_input.get("task", "")))
            if refused:
                return sdk.PermissionResultDeny(message=refused)
        allowed, message = await self.gate.check(agent.id, agent.gate_role, agent.cwd, tool, tool_input, agent.pending)
        if allowed:
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=message)
