"""Who may delegate to whom. Standard library only, no Qt.

Modes (config "orchestration" -> "mode"):
- manual (default): the user picks the agent of each chat; nobody delegates.
- coordinator: the user talks to the coordinator role, which hands each part of a request to another role.
- collaborative: like coordinator, and every role may delegate too, within "allow" and "max_depth".
The coordinator is never a delegation target. "max_delegations" is one budget per message of the user.
"""

MODES = ("manual", "coordinator", "collaborative")
COORDINATOR = "coordinator"
DEFAULTS = {"mode": "manual", "max_depth": 2, "max_delegations": 8, "ask": True, "allow": {}}


def _positive(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 1 else default


def settings(cfg: dict) -> dict:
    raw = cfg.get("orchestration") if isinstance(cfg.get("orchestration"), dict) else {}
    allow = {}
    for role_id, targets in (raw.get("allow") or {}).items() if isinstance(raw.get("allow"), dict) else ():
        if isinstance(targets, list):
            allow[str(role_id)] = [t for t in targets if isinstance(t, str)]
    return {
        "mode": raw.get("mode") if raw.get("mode") in MODES else "manual",
        "max_depth": _positive(raw.get("max_depth"), DEFAULTS["max_depth"]),
        "max_delegations": _positive(raw.get("max_delegations"), DEFAULTS["max_delegations"]),
        "ask": raw.get("ask") if isinstance(raw.get("ask"), bool) else True,
        "allow": allow,
    }


def delegates_for(st: dict, role_id: str, depth: int, roles: dict) -> list:
    """The role ids an agent of `role_id`, `depth` levels below the user's chat, may delegate to."""
    others = [r for r in roles if r not in (role_id, COORDINATOR)]
    if st["mode"] == "coordinator":
        return others if role_id == COORDINATOR and depth == 0 else []
    if st["mode"] == "collaborative" and depth < st["max_depth"]:
        if role_id in st["allow"]:
            return [r for r in st["allow"][role_id] if r in roles and r not in (role_id, COORDINATOR)]
        return others
    return []


def chat_default_role(cfg: dict) -> str:
    """The role of a chat that has not picked one."""
    if settings(cfg)["mode"] == "coordinator":
        return COORDINATOR
    return str(cfg.get("chat_role") or "general")


def delegation_prompt(targets: list, roles: dict, conn_name) -> str:
    """A system-prompt section telling an agent whom it can delegate to (`conn_name(role_id)` names the AI)."""
    lines = [f"- {r}: {roles[r]['title']} (on {conn_name(r)}). {roles[r].get('description', '')}"   # English, like
             for r in targets if r in roles]                                                     # every prompt
    return ("\n\nYou can hand a task to another agent with the Delegate tool (role + a self-contained task: the other "
            "agent sees only the task, not this conversation). It answers once with its result. Delegate when a part "
            "of the request fits another role better; do the rest yourself. Available roles:\n" + "\n".join(lines))


def runtime(cfg: dict, accounts: list, roles: dict, active_account: str, sdk: bool, accounts_root,
            chat_connection: dict) -> dict:
    """What a Runner needs to delegate: the settings, the roles, `resolve(role, parent_connection)` ->
    (connection, model, claude account, config dir) and `conn_name(role_id)` for the prompts.

    A role with its own binding runs on it; a role without one runs on the delegating agent's connection."""
    from pathlib import Path

    from nina import connections

    def bound(role: dict) -> bool:
        return role.get("id") in (cfg.get("role_connections") or {}) or bool(role.get("connection"))

    def resolve(role: dict, parent: dict) -> tuple:
        if bound(role):
            conn, model, _note = connections.resolve(cfg, accounts, explicit=None, role_id=role.get("id", ""),
                                                     role=role, active_account=active_account, sdk=sdk)
        else:
            conn = parent
            model = str(role.get("model") or "") if conn.get("kind") == "claude" else conn.get("model", "")
        account = conn.get("account", "") if conn.get("kind") == "claude" else ""
        config_dir = None
        if account and accounts_root and (Path(accounts_root) / account).is_dir():
            config_dir = str(Path(accounts_root) / account)
        return conn, model, account, config_dir

    def conn_name(role_id: str) -> str:
        role = roles.get(role_id)
        return resolve(role, chat_connection)[0].get("name", "") if role else ""

    return {"settings": settings(cfg), "roles": roles, "resolve": resolve, "conn_name": conn_name}
