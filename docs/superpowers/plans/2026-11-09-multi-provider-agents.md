# Multi-provider Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every NINA agent and chat run on any connection (Claude accounts or any OpenAI-compatible API) with the same tools and the same safety as Claude agents today.

**Architecture:** A `connections` module describes and resolves connections; `secrets` stores keys. `managed.Runner` keeps its API and dispatches per agent to the existing Claude SDK code or to a new `OpenAIAgent` (a tool-calling loop over a stdlib HTTP client) that executes NINA's own `tools` behind one shared permission gate. The Qt UI changes only where an "account" becomes a "connection", plus a Connections dialog.

**Tech Stack:** Python 3.10+, PySide6, standard library (`urllib`, `asyncio`, `json`, `re`, `fnmatch`), `keyring`. Tests: `unittest`, offscreen Qt.

**Spec:** `docs/superpowers/specs/2026-11-09-multi-provider-agents-design.md`

## Global Constraints

- Python >= 3.10; `claude-agent-sdk` stays optional; every new module imports without it.
- Only new dependency: `keyring` (base dependency). HTTP with `urllib` only.
- Tool names exactly: `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `WebFetch`.
- Event kinds unchanged: `status, session, text, tool, denied, permission, model, context, usage, ratelimit, result, error, stderr`; one new kind `hook` (payload = hook-style dict for the agents store).
- Keys never in `config.json`, logs, history, events or the agents DB.
- `price_in`/`price_out` = USD per million tokens. Default HTTP timeout 300 s. Default `max_tool_steps` 40. Permission timeout 600 s (existing).
- Assistant messages from the API are stored and re-sent exactly as received (unknown fields kept).
- No behaviour change for a Claude-only user with no new settings.
- UTF-8 for every file read/written; state files 0600 on POSIX.
- Every user-visible string through `_()` with a pt-BR entry in `src/nina/locales/pt_BR.py`.
- Lint: `python -m pyflakes src tests scripts` clean. Tests: `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t .`

## Review Focus

1. A tool call with invalid JSON arguments or an unknown tool name: the agent gets an error tool result and continues; nothing crashes.
2. A path outside the agent folder given to `Read`/`Glob`/`Grep` (reads are allowed like Claude) vs `Write`/`Edit` (denied by policy): same outcomes as the Claude path.
3. A provider error (401, 404 with message, 429 with Retry-After, timeout, HTML error page): a readable error in the chat with the provider's message, status back to a usable state, no retry loop.
4. Interrupt or close while a request or a tool is running: history stays valid (every tool call has a result) and the next message works.
5. A chat or agent saved with an `account` from before this change, or pointing to a deleted connection: it opens and runs on the right (or default) connection with an info message.

---

### Task 1: Connections, secrets and resolution

**Files:**
- Create: `src/nina/connections.py`, `src/nina/secrets.py`
- Modify: `src/nina/config.py` (DEFAULTS: `"connections": []`, `"default_connection": ""`, `"role_connections": {}`, `"permissions": {"deny": []}`, `"max_tool_steps": 40`), `pyproject.toml` (add `keyring>=24` to dependencies)
- Test: `tests/test_connections.py`

**Interfaces:**
- Produces:
  - `PRESETS: dict[str, dict]` keys `openai, gemini, ollama, openrouter, groq, lmstudio, custom`; each `{"name", "base_url", "needs_key": bool, "key_url": str}`.
  - `claude_connections(accounts: list[str]) -> list[dict]` → `[{"id": "claude", "kind": "claude", "name": "Claude", "account": ""}] + [{"id": f"claude:{s}", "kind": "claude", "name": f"Claude · {s}", "account": s}]`.
  - `all_connections(cfg: dict, accounts: list[str]) -> list[dict]` (Claude first, then valid custom ones; custom entries normalised with defaults `timeout_s=300`, `headers={}`, `key={"source": "none"}`, `context_window=0`, `price_in=0.0`, `price_out=0.0`).
  - `get(cfg, accounts, conn_id) -> dict | None`.
  - `resolve(cfg, accounts, *, explicit: str | None, role_id: str, role: dict, active_account: str, sdk: bool) -> tuple[dict, str, str]` → `(connection, model, note)`; order: explicit > `role_connections[role_id]` (str or `{"connection","model"}`) > `role["connection"]` > `default_connection` > `claude:<active_account>`/`claude`; if no SDK and the result is Claude, first custom connection; unknown id → default + note `"Connection '{id}' not found; using {name}."`. Model: explicit model (when `explicit` came with one — pass via `explicit_model` kwarg) > binding model > for Claude `role["model"]` / for openai `connection["model"]`.
  - `account_to_connection(account: str | None) -> str | None` (`None`/`""` → `None`, `"x"` → `"claude:x"`).
  - `secrets.get_key(conn: dict) -> str` (env: `os.environ`, then on Windows `HKCU\Environment`; keyring: `keyring.get_password("nina", conn["id"])`; none: `""`), `secrets.set_key(conn_id: str, value: str) -> None`, `secrets.delete_key(conn_id) -> None`, `secrets.keyring_usable() -> bool`.

- [ ] **Step 1: Write failing tests** in `tests/test_connections.py`: `test_claude_connections_from_accounts`, `test_custom_connections_are_normalised_and_invalid_ones_skipped` (missing base_url or kind → skipped), `test_resolution_order` (explicit beats binding beats role file beats default beats active account), `test_binding_with_model_override`, `test_role_model_only_applies_to_claude` (role `model: sonnet` + openai connection → connection model), `test_unknown_connection_falls_back_with_note`, `test_without_sdk_claude_resolves_to_first_custom`, `test_account_to_connection`, `test_env_key_and_fake_keyring` (patch `secrets.keyring` with a fake module object).
- [ ] **Step 2: Run** `python -m unittest tests.test_connections -v` → FAIL (module missing).
- [ ] **Step 3: Implement** both modules (stdlib + optional `keyring` import guarded by try/except → `keyring = None`).
- [ ] **Step 4: Run** the test file → PASS; full suite → OK.
- [ ] **Step 5: Commit** `Add connections and secrets (Claude accounts and OpenAI-compatible APIs)`.

### Task 2: Shared permission gate with rule syntax and a deny list

**Files:**
- Create: `src/nina/agents/permissions.py`
- Modify: `src/nina/agents/managed.py` (`decide` moves/re-exports; `Runner._can_use_tool` uses the gate)
- Test: `tests/test_permissions.py` (existing `tests/test_managed.py` must still pass)

**Interfaces:**
- Produces:
  - `rule_matches(rule: str, tool: str, tool_input: dict) -> bool`: `"Read"` matches tool; `"Bash(git log:*)"` matches commands starting with `git log`; `"Bash(ls)"` exact; for other tools the parenthesised part is a glob on `_target_path`/`url`.
  - `decide(tool, tool_input, role, cwd, deny: list[str] = ()) -> tuple[str, str]` same contract as today, deny list first (`("deny", "Blocked by your NINA permissions: {rule}")`), role `tools` matched with `rule_matches`.
  - `class Gate: __init__(self, loop, emit, auto_getter, deny_getter)`; `async check(self, agent_id, role, cwd, tool, tool_input, pending: set) -> tuple[bool, str]` (allow?, message) implementing today's flow (decide → auto mode for Bash → `permission` event → wait 600 s); `resolve(req_id, allow, message, always)`; `remember` handling as today.
- [ ] **Step 1: Failing tests**: `test_rule_syntax`, `test_deny_list_wins_over_role_tools`, `test_gate_allows_preapproved_without_asking`, `test_gate_asks_and_waits_for_resolve`, `test_gate_timeout_denies` (patch timeout small), `test_auto_mode_allows_read_only_bash`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; `Runner` builds one `Gate` and `_can_use_tool` maps `(True, _)` → `PermissionResultAllow()`, `(False, msg)` → `PermissionResultDeny(message=msg)`; `Runner.resolve` delegates to the gate; `Runner.deny` attribute (list) read by the gate.
- [ ] **Step 4: Run** new + `tests.test_managed` → PASS.
- [ ] **Step 5: Commit** `Share one permission gate between agents; Claude-style rules and a NINA deny list`.

### Task 3: Tools

**Files:**
- Create: `src/nina/agents/tools.py`
- Modify: `src/nina/agents/managed.py` (`describe` handles `WebFetch`, `Grep`, `Glob`)
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces:
  - `SPECS: dict[str, dict]` — OpenAI `{"type":"function","function":{name, description, parameters}}` per tool.
  - `offered(role: dict, has_bash: bool) -> list[dict]` (no Write/Edit when `role["writes"]` empty; no Bash when `has_bash` false).
  - `find_bash() -> str | None` (POSIX: `/bin/bash` or `sh`; Windows: Git Bash from `shutil.which("git")`'s install `bin/bash.exe`, else `None`; never WSL's `C:\Windows\System32\bash.exe`).
  - `async run(name: str, args: dict, cwd: str, bash: str | None) -> str` (always returns text; errors as `"Error: ..."`), results clipped to 30 000 chars.
  - Limits: Read 2000 lines / 256 KB, line numbers `f"{n:>6}\t{line}"`; Glob/Grep max 200 results, ignore dirs as `mentions.IGNORED_DIRS`; Bash default timeout 120 s, max 600 s, output stdout+stderr+`exit code N`; WebFetch 1 MB, 20 s, HTML → text (strip script/style/tags), `http`/`https` only.
- [ ] **Step 1: Failing tests** (temp dirs): read with offset/limit and binary file; write creates parents; edit not-found / ambiguous / replace_all; glob order and ignores; grep with glob filter and case-insensitive; bash echo + exit code + timeout (skip when `find_bash()` is None); webfetch against a local `http.server` thread; unknown tool and bad args return `Error:`; `offered` for read-only roles.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (blocking work in `asyncio.to_thread`; Bash via `asyncio.create_subprocess_exec(bash, "-c", cmd, cwd=cwd)`).
- [ ] **Step 4: Run** → PASS on Windows and Linux (WSL).
- [ ] **Step 5: Commit** `Add NINA's own tools for non-Claude agents`.

### Task 4: OpenAI-compatible HTTP client

**Files:**
- Create: `src/nina/agents/openai_client.py`
- Test: `tests/test_openai_client.py` (+ `tests/fake_openai.py`: a threaded `http.server` serving scripted responses and recording requests)

**Interfaces:**
- Produces:
  - `class ApiError(Exception)` with `status: int`, `message: str` (provider message clipped to 400 chars), `retry_after: str`.
  - `chat(base_url, key, model, messages, tools, timeout, headers) -> dict` (the raw JSON response).
  - `list_models(base_url, key, timeout=20, headers=None) -> list[str]` (strips a leading `models/`, sorted).
  - `friendly(err: ApiError) -> str` (`_("The API key was refused ({status}): {message}")` for 401/403, `_("Not found ({status}): {message}")` 404, `_("Quota or rate limit reached{retry}: {message}")` 429, generic otherwise; timeouts → `_("No answer within {seconds}s.")`).
- [ ] **Step 1: Failing tests**: request shape (URL, `Authorization: Bearer` only when key, JSON body), 200 → dict, 401/404/429 → `ApiError` fields (Retry-After), non-JSON error body, timeout, `list_models` stripping `models/`.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `Add a standard-library client for OpenAI-compatible APIs`.

### Task 5: OpenAIAgent, sessions and Runner dispatch

**Files:**
- Create: `src/nina/agents/openai_agent.py`, `src/nina/agents/sessions.py`
- Modify: `src/nina/agents/managed.py` (`Runner.start(..., connection: dict | None = None, model: str = "")`, `available()` unchanged, new `can_run(connection) -> bool`)
- Test: `tests/test_openai_agent.py`

**Interfaces:**
- Consumes: Task 2 `Gate`, Task 3 `tools`, Task 4 `chat`, Task 1 `secrets.get_key`.
- Produces:
  - `sessions.save(session_id, messages, path=None)`, `sessions.load(session_id, path=None) -> list | None`, `sessions.new_id() -> str`, `sessions.prune(keep_days, keep_ids)`; folder `state/nina/sessions/`.
  - `class OpenAIAgent: __init__(self, agent, connection, model, gate, emit, max_steps)`; `async run(self, prompt)` with the same lifecycle as `Runner._launch` (queue, `None` closes, statuses `working/idle/closed`).
  - System prompt built by `system_prompt(role, cwd, has_bash) -> str` (folder, OS name, date, tools, `@path`, reply in the user's language, then role prompt).
  - Events per turn: `session` (first turn), `model`, `text`, `tool`, `denied`, `permission` (via gate), `usage` (prompt+completion tokens), `context` (`{"tokens": prompt_tokens, "max": context_window, "model": model}`), `result` (`{"cost": cumulative USD or None, "turns": steps, "error": False}`), `error`, and `hook` dicts: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `Stop`, `SessionEnd` with `sid`=session id, `cwd`, `tool`, `detail`, `prompt`, `last`.
  - Trimming: when `context_window` > 0 and `len(json.dumps(messages))/4` > 0.8 × window, replace the oldest tool results' content with `"[trimmed]"` until under.
- [ ] **Step 1: Failing tests** against `fake_openai`: plain answer; one tool call (Read) then answer; denied write (role read-only) returns denial text to the model and emits `denied`; permission asked and resolved allow/deny; invalid tool JSON → error result and continue; step limit; interrupt mid-request keeps history valid; resume loads session and sends previous messages; Gemini-style `extra_content` in tool calls is sent back unchanged; 429 → `error` event and status `idle`; images become `image_url` parts; cost from prices.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS (plus `tests.test_managed`).
- [ ] **Step 5: Commit** `Run agents on OpenAI-compatible connections with NINA's tools`.

### Task 6: Wire connections into the chat, the managed tab and the mascot

**Files:**
- Modify: `src/nina/chat.py` (controller: `connection` replaces `account` internally, saved state keys `connection` + legacy `account`; chip lists connections; `hook` events → new signal `hook_event(dict)`), `src/nina/agents/managed_view.py` (dialog "Connection"; saved agents `connection` + legacy `account`), `src/nina/agents/managed_store.py`, `src/nina/widget.py` (`managed_context` adds `connections`, `cfg`, `sdk`; availability = SDK or custom connection; forward `hook_event` to `record_agent_event`), `src/nina/chatinfo.py` (friendly model for non-Claude = raw id; connection name in details)
- Test: extend `tests/test_chat.py`, `tests/test_managed_view.py`, `tests/test_managed_store.py`

**Interfaces:**
- Consumes: Task 1 `resolve`, `account_to_connection`; Task 5 `Runner.start(connection=, model=)`.
- [ ] **Step 1: Failing tests**: chat with an openai connection passes it to `runner.start`; chip lists Claude + custom; changing connection starts a new session with info message; legacy saved `account` maps to `claude:<slug>`; deleted connection falls back with note; managed dialog preselects role binding; managed store round-trips `connection`; info line shows raw model id; chat available with only a custom connection and no SDK.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `Choose a connection per chat and per agent`.

### Task 7: Connections dialog, menu entry and CLI

**Files:**
- Create: `src/nina/connections_view.py`
- Modify: `src/nina/widget.py` (menu "Connections and agents..."), `src/nina/app.py` (`--connections`, `--test-connection <id>`)
- Test: `tests/test_connections_view.py`, extend `tests/test_core.py`

**Interfaces:**
- `ConnectionsDialog(cfg: dict, accounts: list[str], save: Callable[[dict], None], parent=None, client=openai_client, keystore=secrets)`; tabs "Connections" and "Agents". Add/edit form: preset, name, id (slug from name, unique), base URL, model (editable combo + "Fetch models"), key (password field; source keyring or env var name), context window, prices, timeout; "Test" runs `chat(... "Reply with the word OK.")` in a worker thread and shows the reply or `friendly(err)`; "Set as default"; remove deletes the stored key.
- [ ] **Step 1: Failing tests** (offscreen, fake client and keystore): add from preset fills base URL; save writes config without the key and stores the key in the keystore; edit keeps id; remove deletes key and bindings that pointed to it; agents tab writes `role_connections`; CLI lists without secrets.
- [ ] **Step 2–4:** run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `Add the Connections and agents dialog and CLI commands`.

### Task 8: Docs, translations, CI and end-to-end checks

**Files:**
- Modify: `README.md`, `README.pt-BR.md` (section "Connections: other AIs"), `config.example.json`, `src/nina/locales/pt_BR.py` (all new strings; `tests/test_i18n.py` must pass), `.github/workflows/ci.yml` (nothing new expected; keep green)
- [ ] **Step 1:** Write the docs (presets, keys, deny list, model choice, differences from Claude: settings.json rules, permission_mode, quality depends on the model, Bash on Windows needs Git Bash).
- [ ] **Step 2:** Full suite Windows + WSL Linux, pyflakes.
- [ ] **Step 3: Manual end-to-end** (not in CI): Ollama `qwen3:8b` and Gemini (key from `GEMINI_API_KEY`) each: chat reads a file, proposes an edit (Allow), runs a read-only command in auto mode, interrupt, restart NINA and resume; Claude chat unchanged.
- [ ] **Step 4: Commit**, push the branch, open the PR.
