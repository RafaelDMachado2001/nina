"""Connections: the AIs an agent can run on. Standard library only.

Two kinds:
- "claude": the user's Claude Code accounts (the plain ~/.claude and every ~/.claude/accounts/<slug>), driven through
  the Claude Agent SDK. They are discovered, never stored.
- "openai": anything that speaks the OpenAI Chat Completions API with tools (OpenAI, Google Gemini's compatible
  endpoint, Ollama, OpenRouter, Groq, LM Studio, a company server...). Stored in config.json under "connections",
  without the key (see nina.secrets).

`resolve()` picks the connection and the model for one chat or agent.
"""
from pathlib import Path

from nina.i18n import _

KINDS = ("claude", "openai")
DEFAULTS = {"timeout_s": 300, "headers": {}, "key": {"source": "none"}, "context_window": 0,
            "price_in": 0.0, "price_out": 0.0, "model": ""}

# Presets only pre-fill the add dialog. No preset is special anywhere else; any OpenAI-compatible URL works.
PRESETS = {
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "needs_key": True,
               "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "needs_key": True, "key_url": "https://aistudio.google.com/apikey"},
    "ollama": {"name": "Ollama (local)", "base_url": "http://localhost:11434/v1", "needs_key": False,
               "key_url": "https://ollama.com/download"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "needs_key": True,
                   "key_url": "https://openrouter.ai/keys"},
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "needs_key": True,
             "key_url": "https://console.groq.com/keys"},
    "lmstudio": {"name": "LM Studio (local)", "base_url": "http://localhost:1234/v1", "needs_key": False,
                 "key_url": "https://lmstudio.ai"},
    "custom": {"name": "Custom (OpenAI-compatible)", "base_url": "", "needs_key": False, "key_url": ""},
}


def claude_accounts(listed, active: str = "", root=None, also=()) -> list:
    """Claude account slugs: the listed ones, the active one, `also`, and every folder in `root` (when given)."""
    found = {a for a in list(listed or []) + [active] + list(also) if a}
    if root:
        try:
            found |= {d.name for d in Path(root).iterdir() if d.is_dir()}
        except OSError:
            pass
    return sorted(found)


def claude_connections(accounts: list) -> list:
    """The plain ~/.claude, then one connection per account folder."""
    out = [{"id": "claude", "kind": "claude", "name": "Claude", "account": ""}]
    out += [{"id": f"claude:{slug}", "kind": "claude", "name": f"Claude · {slug}", "account": slug} for slug in accounts]
    return out


def _normalise(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    cid, kind, url = str(entry.get("id") or ""), entry.get("kind"), str(entry.get("base_url") or "").strip()
    if not cid or kind != "openai" or not url or cid == "claude" or cid.startswith("claude:"):
        return None
    conn = {**DEFAULTS, **entry, "id": cid, "kind": kind, "base_url": url.rstrip("/")}
    conn["name"] = str(entry.get("name") or cid)
    if not isinstance(conn["key"], dict):
        conn["key"] = {"source": "none"}
    if not isinstance(conn["headers"], dict):
        conn["headers"] = {}
    for field, cast in (("timeout_s", int), ("context_window", int), ("price_in", float), ("price_out", float)):
        try:
            conn[field] = cast(conn[field])
        except (TypeError, ValueError):
            conn[field] = DEFAULTS[field]
    return conn


def custom_connections(cfg: dict) -> list:
    raw = cfg.get("connections") or []
    seen, out = set(), []
    for entry in raw if isinstance(raw, list) else []:
        conn = _normalise(entry)
        if conn is not None and conn["id"] not in seen:
            seen.add(conn["id"])
            out.append(conn)
    return out


def all_connections(cfg: dict, accounts: list) -> list:
    return claude_connections(accounts) + custom_connections(cfg)


def get(cfg: dict, accounts: list, conn_id: str | None) -> dict | None:
    return next((c for c in all_connections(cfg, accounts) if c["id"] == conn_id), None) if conn_id else None


def account_to_connection(account: str | None) -> str | None:
    """A chat or agent saved before connections existed: its Claude account slug as a connection id."""
    return f"claude:{account}" if account else None


def _binding(cfg: dict, role_id: str) -> tuple:
    value = (cfg.get("role_connections") or {}).get(role_id)
    if isinstance(value, dict):
        return str(value.get("connection") or ""), str(value.get("model") or "")
    return (str(value), "") if value else ("", "")


def default_connection(cfg: dict, accounts: list, active_account: str) -> dict:
    conn = get(cfg, accounts, cfg.get("default_connection") or "")
    if conn is not None:
        return conn
    return get(cfg, accounts, account_to_connection(active_account)) or claude_connections([])[0]


def resolve(cfg: dict, accounts: list, *, explicit: str | None, role_id: str, role: dict, active_account: str,
            sdk: bool, explicit_model: str = "") -> tuple:
    """(connection, model, note). Order: explicit > role_connections > role file `connection:` > default_connection >
    the active Claude account. `note` explains a fallback ("" when none)."""
    binding_id, binding_model = _binding(cfg, role_id)
    wanted = [(explicit, explicit_model), (binding_id, binding_model), (str(role.get("connection") or ""), "")]
    conn, model, note = None, "", ""
    for conn_id, candidate_model in wanted:
        if not conn_id:
            continue
        conn = get(cfg, accounts, conn_id)
        if conn is None:
            note = note or conn_id
            continue
        model = candidate_model
        break
    if conn is None:
        conn = default_connection(cfg, accounts, active_account)
    if conn["kind"] == "claude" and not sdk:
        custom = custom_connections(cfg)
        if custom:
            conn, model = custom[0], ""
    if note:
        note = _("Connection '{id}' not found; using {name}.", id=note, name=conn["name"])
    if not model:
        model = str(role.get("model") or "") if conn["kind"] == "claude" else conn.get("model", "")
    return conn, model, note
