# Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let NINA's agents delegate tasks to other roles (on any AI), in modes the user picks: Manual (default), Coordinator, Collaborative.

**Architecture:** A pure `orchestration` module decides who may delegate to whom. The `Runner` gains `delegate()`: it starts a one-shot child agent (any connection) inside the parent's tool call, forwards the child's events to the parent's stream with a label, and returns the child's answer. `Delegate` is a NINA tool on OpenAI-compatible agents and an in-process SDK MCP tool on Claude agents; both go through the shared gate. The chat gains a role chip per tab and an Orchestration menu.

**Tech Stack:** Python 3.10+, PySide6, standard library, optional claude-agent-sdk (`SdkMcpTool`, `create_sdk_mcp_server`). Tests: unittest, offscreen Qt, `tests/fake_openai.py`.

**Spec:** `docs/superpowers/specs/2026-11-09-orchestration-design.md`

## Global Constraints

- Manual mode (default) behaves exactly like Project 1: no `Delegate` tool anywhere, Claude's native subagents untouched.
- Settings: `"orchestration": {"mode": "manual"|"coordinator"|"collaborative", "max_depth": 2, "max_delegations": 8, "ask": true, "allow": {}}`.
- `max_delegations` is one budget per user message, shared by the whole delegation tree.
- The coordinator role id is `coordinator`; it is never a delegation target; read-only (no writes).
- `Delegate` asks Allow/Deny unless `ask` is false; child actions go through the same gate; child events are labelled `"<role title> · <AI name>"`.
- A child without its own role binding runs on the delegating agent's connection.
- Agents that get `Delegate` lose Claude's `Task` and `Agent` tools.
- Panel/graph: children are sub-agents (`aid`) of the root session (`sid`), `atype` = `"<parent role> → <child role>"`.
- Child result returned to the parent clipped to 20 000 characters.
- All user-visible strings through `_()` with pt-BR entries; pyflakes clean; Windows and Linux.

## Review Focus

1. The budget or the depth is exhausted mid-tree: the parent gets a clear error text and the turn continues; nothing hangs.
2. Interrupting or closing the parent while a child waits for a permission: the child is cancelled, its pending request disappears, and the parent's history stays valid.
3. A child on another AI fails (429, bad key): the parent receives the error as the tool result and can answer.
4. The user denies the delegation itself: the parent is told and no child starts.
5. A tab following the default role when the mode changes: the next message starts a new conversation with a note; tabs with an explicit role are untouched.

---

### Task 1: Orchestration rules and the coordinator role

**Files:** Create `src/nina/orchestration.py`; modify `src/nina/agents/roles.py` (BUILTIN `coordinator`), `src/nina/config.py` (DEFAULTS `orchestration`); test `tests/test_orchestration.py`.

**Interfaces (produces):**
- `MODES = ("manual", "coordinator", "collaborative")`
- `settings(cfg) -> dict` normalised (`mode`, `max_depth` int ≥ 1, `max_delegations` int ≥ 1, `ask` bool, `allow` dict of lists); invalid values → defaults.
- `delegates_for(st: dict, role_id: str, depth: int, roles: dict) -> list[str]` per the spec (manual: []; coordinator: only `coordinator` at depth 0 → all non-coordinator roles; collaborative: depth < max_depth → `allow[role]` or all roles except itself and `coordinator`; never `coordinator`; only ids present in `roles`).
- `chat_default_role(cfg) -> str` (`coordinator` in coordinator mode, else `cfg["chat_role"] or "general"`).
- `delegation_prompt(targets: list[str], roles: dict, conn_name: Callable[[str], str]) -> str` (a section listing id, title, description, AI).
- [ ] Failing tests: settings normalisation; delegates_for for each mode/depth/allow; coordinator never a target; chat_default_role; prompt lists targets with AI names.
- [ ] Implement; tests pass; full suite; commit `Add orchestration rules and the coordinator role`.

### Task 2: Runner delegation core and the Delegate tool on OpenAI-compatible agents

**Files:** Modify `src/nina/agents/managed.py`, `src/nina/agents/openai_agent.py`, `src/nina/agents/tools.py` (spec `DELEGATE_SPEC(targets)`), `src/nina/agents/permissions.py` (`describe` for Delegate; tool name normalisation `mcp__nina__delegate` → `Delegate`); test `tests/test_delegation.py`.

**Interfaces:**
- Consumes: Task 1 `orchestration.*`.
- `Runner.orchestration: dict` set by callers: `{"settings": st, "roles": roles, "resolve": callable(role, parent_connection) -> (conn, model), "conn_name": callable(id)->str}`; default = manual.
- `_Agent` gains `depth=0`, `parent=None`, `budget=None` (a shared `{"left": int}`), `label` (`"<title> · <AI>"`).
- `Runner.targets_for(agent) -> list[str]`.
- `async Runner.delegate(parent: _Agent, role_id: str, task: str) -> str` (never raises; error texts start with `"Error:"`).
- Event forwarding of child → parent id: `tool` → `{"name": f"{label} → {name}", "summary"}`; `permission` → payload with `"tool": f"{label} · {tool}"`, same `req`; `denied` likewise; `usage` added; `turn_error`/`error` → returned as error text; `hook` from the child dropped and replaced by `SubagentStart`/`PreToolUse`/`SubagentStop` with `sid` = root session, `aid` = child id, `atype` = `"<parent title> → <child title>"`; child `result` cost → parent `text`? no: an `info`-style `tool` line `"{label}: US$ x"` only when a cost is known.
- The budget is created per top-level turn (depth 0) and shared by children.
- [ ] Failing tests (fake OpenAI server, two scripted conversations routed by model name): manual mode offers no Delegate; coordinator delegates to a role on another connection and receives its answer; delegation asks and a denial reaches the parent; child permission is asked with the label and resolved through `Runner.resolve`; budget of 1 stops the second delegation; depth limit; child 429 returned as error text; interrupting the parent cancels the child and closes its pending request; hook events produce one sub-agent per child under the root session.
- [ ] Implement; tests pass; full suite; commit `Let agents delegate to other roles on any AI`.

### Task 3: Delegate on Claude agents

**Files:** Modify `src/nina/agents/managed.py` (`_options`: `mcp_servers={"nina": server}` with tool `delegate` when `targets_for(agent)`; add `Task`, `Agent` to `disallowed_tools` then); test in `tests/test_delegation.py`.

- [ ] Failing tests (SDK installed; skipped otherwise): options carry the server and the disallowed native tools only when the agent may delegate; the server's handler runs `Runner.delegate` and returns its text; the gate treats `mcp__nina__delegate` as `Delegate` (asks by default, pre-approved when `ask` is false).
- [ ] Implement; tests pass; commit `Let Claude agents delegate through NINA`.

### Task 4: Chat and Managed tab

**Files:** Modify `src/nina/chat.py` (role per tab: `ChatController.role_id` None = follow default; `role_entries()`, `set_role()`; saved session records `role`; mismatch → new conversation + info; role chip next to the AI chip; ⋮ menu "Orchestration" submenu calling `on_orchestration(mode)`); `src/nina/agents/managed_view.py` and chat set `runner.orchestration`; `src/nina/widget.py` (save mode; hand `on_orchestration`); tests in `tests/test_chat.py`, `tests/test_managed_view.py`.
- [ ] Failing tests: default role follows mode; explicit role kept; mode switch → next message new session with note; tabs persist `role`; role chip entries; runner gets orchestration settings with a resolver honouring "child on parent's connection".
- [ ] Implement; tests pass; commit `Pick the agent of each chat and the orchestration mode`.

### Task 5: Settings UI, docs, end-to-end

**Files:** Modify `src/nina/connections_view.py` (Agents tab: Orchestration group: mode, max depth, max delegations, ask), README.md, README.pt-BR.md, config.example.json, pt-BR catalog.
- [ ] Failing dialog test: orchestration fields saved into `cfg["orchestration"]`.
- [ ] Implement; docs; full suite Windows + Linux with/without SDK.
- [ ] Real e2e: coordinator on Gemini delegating to a role on Ollama and to a role on Claude; Claude coordinator delegating to Gemini; deny a delegation; budget exhaustion.
- [ ] Commit, push, PR (base: `feature/multi-provider-agents`).
