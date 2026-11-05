"""
Roles for managed agents. Standard library only.

A role is a prompt + the tools that are pre-approved + where the agent may write. Built-in roles live here; add or
override them by creating Markdown files in ~/.config/nina/roles/<id>.md:

    ---
    title: PR reviewer
    description: Reviews diffs and points out risks
    tools: Read, Glob, Grep
    writes:
    model: sonnet
    ---
    You are a reviewer... (the body is the prompt)

`tools` run without asking. `writes` are the paths (globs relative to the agent's folder) where it may propose
writes; every write still asks for your confirmation. Empty = no writes at all.
"""
from pathlib import Path

from nina.i18n import N_

READ_TOOLS = ["Read", "Glob", "Grep"]
LANGUAGE_RULE = "Reply in the language the user writes in. "

# Titles and descriptions are marked with N_ and translated where they are displayed.
BUILTIN = {
    "general": {
        "title": N_("General assistant"),
        "description": N_("General conversation with read access pre-approved and every write confirmed."),
        "tools": READ_TOOLS, "writes": ["**"],
        "prompt": LANGUAGE_RULE + "You are NINA, the user's assistant. Be direct and concise. A word like @path in the user's message is a "
                  "file or folder they mention: read it (or explore the folder) before answering. Read and search before "
                  "stating facts. Before changing any file, explain what will change.",
    },
    "agent-builder": {
        "title": N_("Agent builder"),
        "description": N_("Interviews you and creates the project's subagents (.claude/agents) and skills, one approved file at a time."),
        "tools": READ_TOOLS,
        "writes": [".claude/agents/*", ".claude/skills/*", ".claude/commands/*"],
        "prompt": (
            LANGUAGE_RULE + "You help the user create agents (Claude Code subagents) for the project in the current folder.\n\n"
            "Process: (1) look at the project and at the agents that already exist (`.claude/agents/`); (2) ask at most "
            "3 short questions about what the agent should do, when it should be triggered and what it must NOT do; "
            "(3) write the file with the Write tool at `.claude/agents/<lowercase-hyphenated-name>.md`; (4) explain in "
            "3 lines how to use it. You may only write in `.claude/agents/`, `.claude/skills/` and `.claude/commands/`.\n\n"
            "File format:\n---\nname: lowercase-hyphenated-name\n"
            "description: a clear sentence on when to delegate to this agent (this decides automatic delegation)\n"
            "tools: Read, Grep, Glob\nmodel: sonnet\n---\nAgent prompt: role, steps, response format and limits.\n\n"
            "Good practice: give the fewest tools (read-only by default; only add Edit, Write or Bash if the job needs "
            "them); a specific description, never a generic one; a short, verifiable prompt; say how the agent "
            "should report its result."),
    },
    "code-reviewer": {
        "title": N_("Code reviewer"),
        "description": N_("Reviews code and diffs: correctness, security, performance and tests. Read-only."),
        "tools": READ_TOOLS, "writes": [],
        "prompt": LANGUAGE_RULE + "You are a rigorous code reviewer. Read the requested code and diff (use `git diff` and "
                  "`git log` when useful) and answer in order of severity: bugs and security flaws first, then risks, "
                  "then improvements. Always cite file:line and propose the fix in a few lines. Do not praise for the "
                  "sake of praising and do not invent problems: if it is correct, say so. You never change files.",
    },
    "writing-reviewer": {
        "title": N_("Writing reviewer"),
        "description": N_("Reviews articles: technical argument, structure, clarity and Markdown, without rewriting in your voice."),
        "tools": READ_TOOLS, "writes": [],
        "prompt": (
            LANGUAGE_RULE + "You review technical blog articles. Preserve the author's voice: suggest, do not rewrite the "
            "whole text. If you do not know the target audience, ask first.\n\nDeliver: (1) a 3-line summary of what the "
            "text argues; (2) problems by severity, each with the quoted passage, why it matters and a short suggestion. "
            "Evaluate: a clear thesis; evidence and sources for every strong claim; ignored counter-arguments; logical "
            "gaps; undefined jargon; structure (introduction, progression, conclusion); term consistency; spelling, "
            "agreement and punctuation; Markdown formatting (headings, lists, code blocks, links). (3) a final list "
            "of up to 5 prioritized actions.")
    },
    "architect": {
        "title": N_("Architect"),
        "description": N_("Analyzes the system, compares alternatives and records decisions (ADRs). Writes only in docs/adr."),
        "tools": READ_TOOLS, "writes": ["docs/adr/*"],
        "prompt": LANGUAGE_RULE + "You are a pragmatic software architect. Understand the code and the constraints before "
                  "giving an opinion; compare at most 3 alternatives with costs, risks and reversibility; recommend one "
                  "and say what would invalidate the recommendation. Once a decision is made, offer to record it as an ADR "
                  "in `docs/adr/NNNN-title.md` (context, decision, consequences).",
    },
    "project-manager": {
        "title": N_("Project manager"),
        "description": N_("Plans deliveries, risks and next steps from what is in the files. Read-only."),
        "tools": READ_TOOLS, "writes": [],
        "prompt": LANGUAGE_RULE + "You are an objective project manager. From the project's files, git history and documents: "
                  "summarize the current state, list risks and blockers with a suggested owner, and propose the next 3 "
                  "steps with a definition of done. Separate fact (with source) from assumption. Never invent dates or status.",
    },
    "product-owner": {
        "title": N_("Product owner"),
        "description": N_("Turns requests into stories with acceptance criteria and priority. Read-only."),
        "tools": READ_TOOLS, "writes": [],
        "prompt": LANGUAGE_RULE + "You are a Product Owner. Turn the request into short user stories (As a... I want... so "
                  "that...), each with testable acceptance criteria, what is out of scope and a priority suggestion "
                  "with a value rationale. Point out the questions that block refinement before assuming answers.",
    },
}


def _split(value: str) -> list:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_role_file(text: str) -> dict | None:
    """Parse a role file (simple `key: value` frontmatter + body = prompt)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    body = "\n".join(lines[end + 1:]).strip()
    if not body:
        return None
    return {"title": meta.get("title", ""), "description": meta.get("description", ""), "prompt": body,
            "tools": _split(meta.get("tools", "Read, Glob, Grep")), "writes": _split(meta.get("writes", "")),
            "model": meta.get("model", ""), "permission_mode": meta.get("permission_mode", "")}


def load_roles(roles_dir: Path | str | None = None) -> dict:
    """Built-in roles + the files in roles_dir (which override by id). Invalid files are ignored."""
    roles = {rid: {"id": rid, "model": "", "permission_mode": "", **r} for rid, r in BUILTIN.items()}
    if roles_dir:
        try:
            files = sorted(Path(roles_dir).expanduser().glob("*.md"))
        except OSError:
            files = []
        for f in files:
            try:
                parsed = parse_role_file(f.read_text())
            except (OSError, ValueError):
                continue
            if parsed:
                parsed["title"] = parsed["title"] or f.stem
                roles[f.stem] = {"id": f.stem, **parsed}
    return roles
