# Orchestration (Project 2 of 2): agents that delegate to agents

Status: approved in conversation on 2026-11-09 (the maintainer delegated the build end to end and decided the default
mode; "builder's call" items were decided without him and are listed for his review). Builds on Project 1
(`2026-11-09-multi-provider-agents-design.md`): connections, roles bound to connections, NINA's own tools, one shared
permission gate.

## Intent

NINA runs on the user's computer, next to their files and their agents. With Project 1 each agent can run on a
different AI; Project 2 lets them work together: the user asks once ("review the text of this article, then fix the
contact form bug") and the right agents do their parts. The maintainer wants this to be something the AI ecosystem
lacks today: an orchestrator the user controls, across AIs and accounts. NINA is public open source, so how
orchestration behaves is chosen by each user, with safe defaults.

## Goals

1. Three modes, chosen by the user:
   - **Manual** (default, today's behaviour): the user picks the agent (role) of each chat; agents never delegate.
   - **Coordinator**: the user talks to a coordinator agent ("NINA") that splits the request and **delegates** each part to
     the right role; it merges the results and answers.
   - **Collaborative**: like Coordinator, and every agent may also delegate to other roles (within the user's limits).
2. In every mode the user can pick the role of a chat (a role chip per chat tab, next to the AI chip).
3. **Delegation is one more action**, under the same rules as every other one:
   - asking "Allow / Deny" before delegating by default (the user can turn that off), naming the role, its AI and the task;
   - every action of the delegated agent goes through the same gate and shows the same sheet, labelled with that agent;
   - a depth limit (default 2) and a limit of delegations per answer (default 8) prevent loops and runaway spending.
4. Delegation works from and to any connection: a Claude agent can delegate to a Gemini agent and the other way round.
5. Visible: the chat shows each delegated step ("Writing reviewer · Gemini → Read: post.md"); the agents panel and graph
   show the delegated agent as a sub-agent of the session that delegated (edge = delegation).
6. Works in the chat and in the Managed tab, on Linux and Windows, with or without the Claude SDK.

## Non-goals

- Agents running in parallel (a delegation waits for its result; parallel fan-out is future work).
- Long-lived conversations between agents (a delegated agent gets one task, answers once, and closes).
- Automatic routing without an LLM (keyword rules, embeddings).
- A visual workflow editor.

## Anti-goals

- A delegated agent with more power than the same role has in a chat of its own.
- Delegation that spends money silently: by default every delegation asks; each delegated agent's cost/tokens are shown.
- A change for users who never turn orchestration on (Manual mode = exactly Project 1's behaviour).
- Infinite or exponential delegation.

## Design

### Settings (`config.json`, all optional)

```json
"orchestration": {"mode": "manual", "max_depth": 2, "max_delegations": 8, "ask": true,
                  "allow": {"<role id>": ["<role id>", ...]}}
```

- `mode`: `manual` | `coordinator` | `collaborative`.
- `max_depth`: how many levels below the user's chat may exist (coordinator → agent = 1; agent → agent = 2).
- `max_delegations`: per message of the user, shared by every agent it sets off (see decision 1).
- `ask`: ask Allow/Deny before each delegation (default true). When false, `Delegate` is pre-approved for every role.
- `allow`: in collaborative mode, which roles each role may delegate to (missing = all except itself and the coordinator).

### Coordinator role

A new built-in role `coordinator` ("NINA (coordinator)"): read-only tools pre-approved, no writes, prompt: understand the
request, ask the user only when it is truly ambiguous, split it, delegate each part with a self-contained task (the
delegated agent sees only the task), then merge and answer. Its system prompt lists the roles it may delegate to with
their descriptions and the AI each runs on. It runs on its own binding (`role_connections["coordinator"]`) like any
role. Users may override it with `~/.config/nina/roles/coordinator.md`.

### Who gets the Delegate tool

Pure function `orchestration.delegates_for(settings, role_id, depth, roles) -> list[role ids]`:
- manual: none;
- coordinator: the coordinator role at depth 0 gets every other role; nobody else delegates;
- collaborative: any role at depth < max_depth gets `allow[role]` (default: all roles except itself and the
  coordinator).
An agent with an empty list does not get the tool at all.

### The Delegate tool

Same name on every connection: `Delegate` with `{"role": <one of the allowed ids>, "task": <text>}`. On Claude it is an
in-process SDK MCP tool (`mcp__nina__delegate`), which the gate treats as `Delegate`.

Running a delegation (`Runner.delegate(parent, role_id, task) -> str`):
1. checks the role is allowed for the parent and the per-answer count; otherwise returns an error text to the parent;
2. resolves the role's connection and model with the same resolver the chat uses (binding → default);
3. starts a child agent (same folder, depth + 1, its own session) with the task plus a short note ("you were delegated
   by <role>; reply with the result; you cannot ask the user questions");
4. forwards the child's events to the parent's stream, labelled: `tool` lines as "<role> · <AI> → <tool>: <summary>",
   `permission` and `denied` with the label (answered through the same `resolve`), `usage` added to the parent's;
   hook events `SubagentStart` / `PreToolUse` / `SubagentStop` with `sid` = the parent's session and `aid` = the child;
5. when the child finishes its answer, closes it and returns its final text (clipped to 20 000 characters) as the tool
   result; an error or a denial becomes an error text the parent can react to;
6. interrupting or closing the parent cancels the child.

### UI

- Chat: a **role chip** per tab (next to the AI chip) lists roles; changing it starts a new conversation (like changing
  the AI). New tabs start on the chat role (`chat_role`, or `coordinator` in coordinator mode). The chat menu (⋮) gets
  "Orchestration" ▸ Manual / Coordinator / Collaborative.
- Connections and agents → Agents tab: an "Orchestration" group: mode, max depth, max delegations, "Ask before
  delegating".
- Managed tab: agents follow the same mode; their delegated steps appear in their log.

### Compatibility

Manual is the default; with it nothing changes. Saved tabs without a role keep using the chat role.

## Decisions after the builder check (maintainer away; builder's calls)

1. **One delegation budget per user message.** `max_delegations` (default 8) counts every delegation in the whole
   tree started by one message of the user, shared by the root agent and all its descendants; it resets at the next
   message. (Per-agent counters would allow 8 + 8×8 agents.)
2. **Tabs follow a default role unless the user picks one.** A tab saves its role only when chosen on the chip; otherwise
   it follows the default (`chat_role`, or `coordinator` in coordinator mode). Switching the mode changes that default:
   a following tab whose saved session belongs to another role starts a new conversation at its next message, with an
   info line. Managed agents: the new-agent dialog keeps its default (General assistant); the coordinator role is
   selectable there; whether an agent may delegate follows the mode as in "Who gets the Delegate tool".
3. **An agent that can delegate through NINA loses Claude's native subagent tools** (`Task`, `Agent`) so that every split
   goes through NINA's limits, prompts and cost lines. Agents that cannot delegate (all of them in Manual mode) keep
   Claude's behaviour unchanged.
4. **A delegated role without its own binding runs on the delegating agent's connection** (and Claude account), not on
   the global default: the user's choice for that conversation also pays for its parts.
5. **Panel and graph: flat.** Every delegated agent, at any depth, is a sub-agent of the root session, labelled
   "<delegating role> → <role>". The child's own session does not appear as a separate top-level entry for agents NINA
   runs itself (a Claude child still fires Claude Code's own hooks if the user installed them; documented).
6. **The coordinator is never a delegation target.**

## Builder's calls (for review)

1. Delegation is sequential and one-shot (the child answers once and closes).
2. `Delegate` asks before running by default (`ask: true`), because it can spend on a paid AI.
3. Default `max_depth` 2, `max_delegations` 8.
4. The coordinator is read-only; it reaches files through the roles it delegates to.
5. Child cost/tokens are added to the parent chat's token count and shown as an info line per delegation.
6. The role chip is per tab; changing it starts a new conversation.

## Left to the builder

Module boundaries, exact prompt wording (EN; prompts are not translated, like the existing roles), UI layout, tests.
