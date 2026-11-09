# Multi-provider agents (Project 1 of 2): connections, agents per connection, tool parity

Status: approved in conversation on 2026-11-09 (the maintainer delegated the build end to end; decisions marked
"builder's call" were made without him and are listed at the end for his review).

## Intent

NINA today drives one kind of AI: Claude, through the Claude Agent SDK (the user's Claude Code accounts). Users have
other AIs too (OpenAI/GPT, Google Gemini, local models through Ollama, OpenRouter, a company server...) and want each
agent on the AI that suits its job best or costs least. The maintainer's own example: the developer agent on Claude
Code, the article reviewer on GPT/Gemini. That example is one use case among many: NINA is public open source, so
nothing here may be specific to one user, provider, project or operating system.

Project 1 builds the base: **connections** (the AIs/accounts a user has), **agents (roles) bound to a connection**,
and **full parity**: an agent on any connection can read, search, create and edit files, run commands and fetch web
pages, under exactly the same safety rules as a Claude agent today. Project 2 (orchestration: a coordinator agent and
delegation between agents) is designed separately and builds on this; Project 1 must not block it.

## Goals

1. A user can register any number of connections. Two kinds:
   - **Claude** (existing): the plain `~/.claude` and every account in `~/.claude/accounts/<slug>`. Discovered
     automatically, never edited in NINA.
   - **OpenAI-compatible**: anything that speaks the OpenAI Chat Completions API (`POST {base_url}/chat/completions`
     with `tools`). This covers OpenAI, Google Gemini (its OpenAI-compatible endpoint), Ollama, OpenRouter, Groq,
     LM Studio, vLLM and others. A connection is: name, base URL, model, API key (optional), and optional extras.
2. Each role can be bound to a connection; each chat tab and each managed agent can pick its connection.
3. An agent on an OpenAI-compatible connection has the same capabilities and the same safety as a Claude agent:
   same tool names, same pre-approval rules, same write-path policy, same Allow/Deny sheet, same auto mode and
   "Allow always" rules, same states in the mascot, same chat features (tabs, history, resume after restart,
   images, @mentions, info line), and it shows up in the agents panel and graph.
4. A user who only has Claude notices no difference and needs no new configuration.
5. A user with no Claude at all (no Claude Code, no `agents` extra) can use the chat and managed agents with an
   OpenAI-compatible connection alone.
6. Works the same on Linux and Windows. Documented in both READMEs.

## Non-goals (Project 1)

- Orchestration: a coordinator, agents delegating to agents, routing. (Project 2.)
- Provider-specific APIs other than the OpenAI-compatible one (native Gemini API, Anthropic API with a key,
  OpenAI Responses API), and vendor CLIs (Codex CLI, Gemini CLI) as backends. The connection model must leave room
  for more kinds later.
- Token-by-token streaming in the UI (the Claude path does not stream either: whole text blocks are shown).
- Automatic fallback to another connection when one fails or runs out of quota.
- Context compaction/summarisation beyond a simple safety trim (see "Long conversations").

## Anti-goals (failure even if it "works")

- A non-Claude agent that can do something without the user's approval that a Claude agent in the same role could
  not. Safety must be identical, enforced by the same code.
- API keys in plain text in `config.json`, in logs, in the chat history, or in the agents database.
- Anything tailored to one provider, one model, one user's paths or one OS in code or defaults.
- Silently spending: switching to a paid connection, or retrying a failed paid request in a loop.
- A regression for current Claude users (behaviour, saved chats, saved managed agents, settings).

## Constraints

- Python 3.10+, PySide6, standard library first. The OpenAI-compatible client uses the standard library
  (`urllib`), no `openai`/`httpx` dependency. New third-party dependency allowed: `keyring` (secrets).
- `claude-agent-sdk` stays optional (the `agents` extra). Everything provider-neutral must import without it.
- The Qt UI talks to agents only through the existing `Runner` contract: `start/send/interrupt/stop/resolve/shutdown`
  plus events `emit(agent_id, kind, payload)` with the existing kinds (`status`, `session`, `text`, `tool`,
  `denied`, `permission`, `model`, `context`, `usage`, `ratelimit`, `result`, `error`, `stderr`). New backends emit
  the same kinds; the UI gains as little provider-specific code as possible.
- Tests: `python -m unittest`, offscreen Qt, no network in the default suite (a local fake HTTP server is fine).
  Lint with pyflakes. CI on Linux (3.10, 3.12, 3.13) and Windows.

## Design

### Connections (`nina/connections.py`, standard library)

A connection is a dict:

```json
{"id": "gemini", "name": "Gemini (free)", "kind": "openai",
 "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
 "model": "gemini-flash-latest",
 "key": {"source": "keyring"} | {"source": "env", "name": "GEMINI_API_KEY"} | {"source": "none"},
 "context_window": 1000000, "price_in": 0.0, "price_out": 0.0,
 "timeout_s": 300, "headers": {}}
```

- Custom connections live in `config.json` under `"connections": [...]`. Never the key itself.
- Claude connections are synthesized, not stored: id `claude` (plain `~/.claude`) and `claude:<slug>` per account
  folder, kind `claude`. The existing `"account"` setting keeps meaning "the active Claude account".
- `"default_connection"`: the connection used when nothing more specific is set. Default: the active Claude account
  (today's behaviour). If the Claude SDK is not installed and custom connections exist, the first custom one.
- `"role_connections": {"<role id>": "<connection id>"}`: maps built-in or custom roles to connections without
  editing role files. A role file may also say `connection: <id>` in its frontmatter; the config mapping wins.
- Resolution order for an agent: explicit choice (chat chip / new-agent dialog) > `role_connections` > role file
  `connection:` > `default_connection`. A reference to a connection that no longer exists falls back to the default
  with a visible info message.
- Presets only pre-fill the add dialog (name, base URL, a suggested model, whether a key is needed, a link to where
  to get one): OpenAI, Google Gemini, Ollama (local), OpenRouter, Groq, LM Studio (local), Custom. No preset is
  special in code.
- Model list: "Fetch models" calls `GET {base_url}/models` (works on OpenAI, Gemini, Ollama, OpenRouter, Groq);
  the field stays free text when the call fails.

### Secrets (`nina/secrets.py`)

- `keyring` (Windows Credential Manager, macOS Keychain, Linux Secret Service) under service `nina`, username =
  connection id. `{"source": "env", "name": ...}` reads an environment variable instead (CI, headless Linux, users
  who already keep keys in their environment). On Windows an env var set with `setx` after NINA started is read
  from `HKCU\Environment` so it works without restarting.
- When no keyring backend is usable, the dialog says so and offers the environment variable option. Keys are never
  written to disk by NINA, never logged, and `redact()` already masks key-like strings in events.

### Agent backends

`managed.Runner` keeps its public API and gains one optional argument: `start(..., connection=None)`. It dispatches
per agent: kind `claude` (or `None`) runs the existing Claude SDK code unchanged; kind `openai` runs the new
`OpenAIAgent`. Both share one permission gate.

**Shared permission gate** (extracted from `Runner._can_use_tool`): `decide()` by the role's policy (pre-approved
tools allowed, writes outside the role's paths denied without asking), then auto mode for shell commands, then a
`permission` event and wait for `resolve()` (600 s timeout = deny). Returns allow/deny + message. The Claude backend
wraps the result in SDK permission objects; the OpenAI backend uses it directly. One implementation, one test suite.

**OpenAIAgent** (`nina/agents/openai_agent.py`): a tool-calling loop on the Runner's asyncio thread.
- Each turn: append the user message; loop { POST chat/completions (non-streaming) with `messages` + `tools`;
  emit `usage`/`context`; emit `text` for the assistant content; for each tool call: emit `tool`, run it through
  the gate, execute it (allowed) or return the denial text (denied), append the tool result } until the model answers
  without tool calls, or a step limit (default 40 per turn, configurable) is hit, which ends the turn with a visible
  note. Then emit `result` (cost if prices are set) and `status idle`.
- HTTP in `asyncio.to_thread` with the connection timeout. Errors become readable `error` events: 401/403 (key),
  404 (model or URL), 429 (quota/rate limit, with Retry-After and the provider's message), 5xx, timeouts, invalid
  JSON. No automatic retries of a failed request. A 429 is also emitted as `ratelimit`-like info so the info line
  shows it.
- Interrupt cancels the pending request/tool; the history is kept consistent (every tool call gets a result,
  "interrupted by the user").
- System prompt: a NINA base prompt (the folder, the OS, today's date, how to use the tools, that `@path` in the
  user's message names a file to read, reply in the user's language) + the role's prompt.
- Images: user images are sent as `image_url` data URLs; a model that rejects them yields a normal error.
- Sessions: the message list is saved after each turn to `~/.local/state/nina/sessions/<session id>.json` (0600,
  UTF-8). The `session` event carries the id, so chat tabs and managed agents save and resume it exactly like a
  Claude session. Old sessions are pruned with the chat history.
- Long conversations: before each request, if the connection declares `context_window`, old tool results are
  shortened (oldest first) to keep the estimated size under ~80% of it. Builder's call; real compaction is future
  work.
- The agent also records hook-like events (session start, prompt, tool use, stop, end) into the agents store, so it
  appears in the agents panel and graph like a Claude Code session.

**Tools** (`nina/agents/tools.py`, standard library, pure where possible), named exactly like Claude Code's so
roles, `decide()`, `describe()`, auto mode and the UI work unchanged:

| Tool | Parameters | Notes |
|---|---|---|
| `Read` | `file_path`, `offset?`, `limit?` | numbered lines, 2000 lines / 256 KB max, binary detection |
| `Write` | `file_path`, `content` | creates parent folders |
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all?` | fails if not found or ambiguous |
| `Glob` | `pattern`, `path?` | sorted by modification time, ignores `.git`, `node_modules`... |
| `Grep` | `pattern`, `path?`, `glob?`, `case_insensitive?`, `output_mode?` | Python regex, same ignores, limited output |
| `Bash` | `command`, `timeout_s?` | POSIX: `/bin/bash -c` (or `sh`); Windows: Git Bash when found (what Claude Code itself requires there), otherwise the tool is not offered |
| `WebFetch` | `url` | GET, text extracted from HTML, size-limited; asks like any non-pre-approved tool |

Relative paths resolve against the agent's folder. Every result is size-limited before going back to the model.
`describe()` gains `WebFetch`. Read-only roles do not get `Write`/`Edit` offered at all (same as the Claude path's
`disallowed_tools`).

### UI

- **Connections dialog** (right click → "Connections and agents..."): tab "Connections" lists Claude accounts
  (read-only) and custom connections (add from a preset, edit, remove, test, set as default); tab "Agents" lists
  roles with a connection selector each ("default" + every connection). "Test" sends a one-word request and shows
  the answer or the error.
- **Chat**: the account chip becomes a connection chip listing Claude accounts and custom connections. Changing it
  starts a new session, as changing accounts does today. Saved chats with an `account` keep working (mapped to
  the matching Claude connection).
- **New agent dialog**: "Account" becomes "Connection", preselected from the role's mapping.
- **Info line and details**: the model name (friendly for Claude, raw id otherwise), context % when the window is
  known, tokens, and cost when prices are set; connection name in the details.
- The chat and the managed tab are available when the Claude SDK is installed **or** at least one custom
  connection exists.

### CLI

- `nina --connections` lists connections (no secrets). `nina --test-connection <id>` sends a one-word request.

## Compatibility

- Existing config, chats (`chats.json`, per-tab state with `account`), managed agents (`managed.json` with
  `account`), roles and hooks keep working unchanged. New fields are optional.

## Testing

- Unit: connection resolution, presets, secrets (fake keyring), each tool (temp folders), the permission gate,
  error mapping, session save/load, trimming.
- Integration: `OpenAIAgent` against a local fake OpenAI-compatible HTTP server (tool calls, denials, interrupt,
  429, images), and the Runner dispatch with the chat controller.
- Manual end-to-end (maintainer's machine, not in CI): Ollama with a tool-capable local model, Gemini free tier
  with a key from the environment, Claude subscription; on Windows and Linux (WSL).

## Decisions taken after the builder check (maintainer away; all marked as builder's calls)

**Model resolution.** A connection has a default `model`. A role's `model:` (e.g. `sonnet`) is a Claude model and
only applies on Claude connections. A binding can override the model: `role_connections` values are either a
connection id or `{"connection": "<id>", "model": "<model>"}`, and the chat chip / new-agent dialog may set a model
for that chat or agent. Order for non-Claude kinds: explicit chat/agent model > binding model > connection model.
For Claude: explicit > binding > role `model:` > Claude's own default.

**Safety layers.** The shared gate covers everything NINA controls, for both kinds:
- `decide()` gains Claude Code's rule syntax for `tools`: `Tool` or `Tool(prefix:*)` / `Tool(exact)` (for Bash,
  matched against the command), so roles such as `tools: Read, Bash(git log:*)` mean the same on every connection.
- A NINA-level deny list, `"permissions": {"deny": ["Bash(rm -rf:*)", "WebFetch", ...]}` in `config.json`, is
  checked first on every connection (and on the Claude path inside `can_use_tool`).
- A role's `permission_mode` other than `default` is honoured only on Claude connections; on other kinds NINA
  always uses the gate (the stricter behaviour), and the role description in the UI says so.
- Claude Code's own `settings.json` permission rules are not read for non-Claude agents (they are Claude Code
  configuration and many users have none). This can only make a non-Claude agent ask where Claude would allow
  silently; to also get Claude's hard denials, the user puts them in NINA's deny list. Documented in the READMEs.

**Default connection vs. the Claude account switcher.** Today a chat with no account of its own follows the mascot's
"Claude account" menu; that stays exactly so. `"default_connection"` unset (the default) means "follow the active
Claude account" (today's behaviour, no migration). When set to an id, that fixed connection is the default for chats
and agents without a choice; the Claude account menu then only affects Claude connections and the terminal launcher.
A chat tab with no explicit choice uses the chat role's binding (`role_connections`) first, then the default.
Saved tabs and managed agents with `account: "<slug>"` map to `claude:<slug>`; `null`/`""` keep following.

**Prices.** `price_in` / `price_out` are US dollars per million tokens (what providers publish).

**Findings from real endpoints (2026-11-09).**
- Gemini's OpenAI-compatible endpoint returns provider data inside tool calls (`extra_content.google.
  thought_signature`) that must be sent back unchanged in the next request. Rule: the assistant message is stored
  and re-sent exactly as received (all unknown fields kept); NINA never rebuilds it from parsed parts.
- `GET /models` on Gemini returns ids prefixed with `models/` and includes models that are refused for new users
  (404 with a message naming the replacement). The model picker strips `models/`; provider error messages are shown
  to the user verbatim (clipped), never replaced by a generic text.
- Model names go stale quickly: presets suggest no fixed model for remote providers; the user picks from "Fetch
  models". (Ollama: the installed models.)
- Response times vary from under a second (Gemini lite) to minutes (a local model loading, a thinking model on a free
  tier): default timeout 300 s, per connection.

## Builder's calls (for review)

1. Non-streaming requests (simpler, same UX as the Claude path today).
2. Standard-library HTTP; `keyring` as the only new dependency (base dependency, small, pure Python).
3. Env-var key source as a first-class alternative to the keyring.
4. Tool names identical to Claude Code's, so roles and safety rules are shared verbatim.
5. Bash on Windows only through Git Bash; no PowerShell tool in Project 1.
6. 40 tool steps per turn by default; `"max_tool_steps"` in config.
7. Context trimming of old tool results when `context_window` is known; no summarisation.
8. Sessions of non-Claude agents stored by NINA in the state folder (0600), pruned with the history.
9. Default connection remains the active Claude account; with no SDK installed, the first custom connection.
10. Presets: OpenAI, Gemini, Ollama, OpenRouter, Groq, LM Studio, Custom; none special-cased in code.

## Left to the builder

File/module boundaries beyond the ones named, exact dialog layouts, wording (EN + pt-BR catalog), and test layout.
