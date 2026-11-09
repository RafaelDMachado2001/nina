"""An agent on an OpenAI-compatible connection: a tool-calling loop that runs NINA's own tools behind the shared
permission gate, on the Runner's asyncio thread. It emits the same events as a Claude agent, plus `turn_error` (an
answer failed but the agent stays ready, its history untouched) and `hook` (hook-style events for the agents panel).

The assistant messages are stored and re-sent exactly as the API returned them: some providers put data the next
request needs inside them (e.g. Gemini's `extra_content.google.thought_signature` in tool calls).
"""
import asyncio
import json
import platform
import re
from datetime import date

from nina import secrets
from nina.agents import openai_client, sessions, tools
from nina.agents.store import clip, redact
from nina.i18n import _

THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def system_prompt(role: dict, cwd: str, has_bash: bool) -> str:
    shell = ("You can run shell commands with the Bash tool (bash)." if has_bash
             else "There is no shell on this computer: you cannot run commands.")
    return (
        f"You are an agent working through NINA, a desktop assistant, on the user's computer ({platform.system()}). "
        f"Today is {date.today().isoformat()}. Your working folder is {cwd}; relative paths are relative to it.\n"
        "Use the tools to read, search and change files instead of guessing; read a file before changing it. "
        f"{shell} Every action that is not pre-approved asks the user first: if one is denied, do not insist, "
        "say what you would have done. A word like @path in the user's message is a file or folder they mention: "
        "read it. Reply in the language the user writes in. Be direct and concise.\n\n"
        + str(role.get("prompt") or "")
    )


def user_content(message):
    """A chat message (str, or {"text", "images": [{"media_type", "data"}]}) as OpenAI content."""
    if not isinstance(message, dict):
        return str(message)
    images = message.get("images") or []
    text = str(message.get("text") or "")
    if not images:
        return text
    parts = [{"type": "text", "text": text}] if text.strip() else []
    parts += [{"type": "image_url", "image_url": {"url": f"data:{i['media_type']};base64,{i['data']}"}} for i in images]
    return parts


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


class OpenAIAgent:
    def __init__(self, agent, connection: dict, model: str, gate, emit, max_steps: int = 40, hide_env=(),
                 delegate=None, targets=(), extra_prompt: str = "", precheck=None):
        self.agent, self.conn, self.model = agent, connection, model or connection.get("model", "")
        self.gate, self.emit, self.max_steps = gate, emit, max(1, int(max_steps or 40))
        self.bash = tools.find_bash()
        self.tools = tools.offered(agent.role, self.bash is not None)
        self.delegate, self.extra_prompt, self.precheck = delegate, extra_prompt, precheck
        if delegate is not None and targets:
            self.tools.append(tools.delegate_spec(list(targets)))
        self.tool_names = {t["function"]["name"] for t in self.tools}
        self.messages = []
        self.turn = None
        self.interrupted = False
        self.cost = 0.0
        self.priced = bool(connection.get("price_in") or connection.get("price_out"))
        own = (connection.get("key") or {})
        self.hide_env = set(hide_env) | ({own["name"]} if own.get("source") == "env" and own.get("name") else set())

    # ---- called by the Runner -------------------------------------------------------------------
    async def interrupt(self) -> None:
        if self.turn is not None and not self.turn.done():
            self.interrupted = True
            self.turn.cancel()

    def _hook(self, ev: str, **fields) -> None:
        data = {"ev": ev, "sid": self.agent.session_id, "cwd": self.agent.cwd, **fields}
        self.emit(self.agent.id, "hook", {k: v for k, v in data.items() if v not in (None, "")})

    async def run(self, prompt) -> None:
        a = self.agent
        a.queue = asyncio.Queue()
        a.task = asyncio.current_task()
        a.client = self
        started = False
        try:
            if a.session_id:
                loaded = sessions.load(a.session_id)
                if loaded is None:
                    raise RuntimeError(_("The saved conversation could not be read."))
                self.messages = loaded
                self._close_open_calls(_("Interrupted."))      # a session saved mid-tool (e.g. a crash) stays usable
            else:
                a.session_id = sessions.new_id()
                sessions.save(a.session_id, [])                # resumable even if NINA quits before the first answer
            self.emit(a.id, "session", a.session_id)
            self.emit(a.id, "model", self.model)
            self._hook("SessionStart")
            started = True
            text = prompt
            while True:
                if text:
                    await self._run_turn(text)
                self.emit(a.id, "status", "idle")
                text = await a.queue.get()          # None = close
                if text is None:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.emit(a.id, "error", clip(f"{type(e).__name__}: {e}", 300))
        finally:
            if started:
                self._hook("SessionEnd", reason="closed")
            a.client = None
            self.emit(a.id, "status", "closed")

    # ---- one answer ---------------------------------------------------------------------------------
    async def _run_turn(self, message) -> None:
        a = self.agent
        self.emit(a.id, "status", "working")
        snapshot = len(self.messages)
        self.interrupted = False
        self.turn = asyncio.ensure_future(self._turn(message))
        try:
            await self.turn
        except asyncio.CancelledError:
            self._close_open_calls(_("Interrupted by the user."))     # before any save: the history must stay valid
            if not self.interrupted:
                raise                               # the agent is being closed
            self.emit(a.id, "turn_error", _("Interrupted."))
        except openai_client.ApiError as e:
            del self.messages[snapshot:]            # a failed answer leaves nothing behind: sending again is clean
            self.emit(a.id, "turn_error", openai_client.friendly(e))
        except Exception as e:
            del self.messages[snapshot:]
            self.emit(a.id, "turn_error", clip(f"{type(e).__name__}: {e}", 300))
        finally:
            self.turn = None
            try:
                sessions.save(a.session_id, self.messages)
            except (OSError, ValueError) as e:
                self.emit(a.id, "stderr", clip(f"could not save the session: {e}", 300))

    def _close_open_calls(self, note: str) -> None:
        """Every tool call in the history must have a result, or the next request is rejected."""
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for m in list(self.messages):
            calls = (m.get("tool_calls") or []) if m.get("role") == "assistant" else []
            for call in calls:
                if call.get("id") not in answered:
                    self.messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                          "name": call.get("function", {}).get("name", ""), "content": note})
                    answered.add(call.get("id"))

    def _trim(self) -> None:
        window = int(self.conn.get("context_window") or 0)
        if window <= 0:
            return
        for m in self.messages:
            if len(json.dumps(self.messages, ensure_ascii=False)) / 4 <= 0.8 * window:
                return
            if m.get("role") == "tool" and m.get("content") != "[trimmed]":
                m["content"] = "[trimmed]"

    async def _turn(self, message) -> None:
        a = self.agent
        self.messages.append({"role": "user", "content": user_content(message)})
        self._hook("UserPromptSubmit", prompt=clip(_text_of(self.messages[-1]["content"]), 160))
        key = secrets.get_key(self.conn)
        system = {"role": "system", "content": system_prompt(a.role, a.cwd, self.bash is not None) + self.extra_prompt}
        steps, last_text = 0, ""
        while True:
            self._trim()
            data = await asyncio.to_thread(
                openai_client.chat, self.conn["base_url"], key, self.model, [system] + self.messages, self.tools,
                float(self.conn.get("timeout_s") or 300), self.conn.get("headers") or {})
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices or not isinstance(choices[0].get("message"), dict):
                raise openai_client.ApiError(200, _("The answer had no message."))
            msg = choices[0]["message"]
            self._usage(data.get("usage") or {})
            self.messages.append(msg)
            shown = THINK.sub("", _text_of(msg.get("content"))).strip()
            if shown:
                last_text = shown
                self.emit(a.id, "text", redact(shown))
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                self.messages.append(await self._call(call))
            steps += 1
            if steps >= self.max_steps:
                self.emit(a.id, "text", _("Stopped after {steps} tool steps in one answer. Send a message to continue.",
                                          steps=steps))
                break
        self.emit(a.id, "result", {"cost": self.cost if self.priced else None, "turns": steps, "error": False,
                                   "reason": None, "errors": []})
        self._hook("Stop", last=clip(last_text, 300))

    async def _call(self, call: dict) -> dict:
        a = self.agent
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        result = {"role": "tool", "tool_call_id": call.get("id"), "name": name}
        try:
            raw = fn.get("arguments")
            args = raw if isinstance(raw, dict) else json.loads(raw or "{}")   # some servers send an object
            if not isinstance(args, dict):
                raise ValueError("not an object")
        except (ValueError, TypeError) as e:
            return {**result, "content": f"Error: the arguments are not a valid JSON object ({e})"}
        self.emit(a.id, "tool", {"name": name, "summary": _summary(args)})
        self._hook("PreToolUse", tool=name, detail=_summary(args))
        if name not in self.tool_names:
            return {**result, "content": f"Error: the tool {name!r} is not available here"}
        if name == "Delegate" and self.precheck is not None:
            refused = self.precheck(str(args.get("role", "")), str(args.get("task", "")))
            if refused:
                return {**result, "content": refused}
        allowed, why = await self.gate.check(a.id, getattr(a, "gate_role", a.role), a.cwd, name, args, a.pending)
        if not allowed:
            return {**result, "content": f"Error: denied: {why}"}
        if name == "Delegate":
            return {**result, "content": await self.delegate(str(args.get("role", "")), str(args.get("task", "")))}
        return {**result, "content": await tools.run(name, args, a.cwd, self.bash, self.hide_env)}

    def _usage(self, usage: dict) -> None:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        if prompt or completion:
            self.emit(self.agent.id, "usage", prompt + completion)
            self.emit(self.agent.id, "context", {"tokens": prompt + completion,
                                                 "max": int(self.conn.get("context_window") or 0), "model": self.model})
        self.cost += (prompt * float(self.conn.get("price_in") or 0) + completion * float(self.conn.get("price_out") or 0)) / 1e6


def _summary(args: dict) -> str:
    if args.get("role") and args.get("task"):                       # Delegate
        return clip(f"{args['role']}: {args['task']}", 120)
    for key in ("command", "file_path", "path", "pattern", "url"):
        if args.get(key):
            return clip(args[key], 120)
    return ""
