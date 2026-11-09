"""
Auto mode for the chat: read-only shell commands run without asking; everything else still asks.

`analyze(command)` splits a shell command into its parts (`;`, `&&`, `||`, `|`, `$(...)`, `ssh host "..."`, `bash -c`)
and returns the *steps* that are not read-only. No steps = the command only reads. A step has a `prefix` (the command
it starts with, e.g. "ssh server") and an `op` (what it does there, e.g. "UPDATE"). "Allow always" saves (prefix, op),
so `ssh server` + UPDATE stays allowed while `ssh server` + DELETE still asks. A `hard` step (an UPDATE/DELETE without
WHERE, a redirect to a file, sudo, a command that could not be parsed) always asks and is never saved.

Pure standard library: no Qt, no SDK.
"""
import json
import os
import re
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path

MAX_DEPTH = 3

SQL_CLIENTS = {"psql", "sqlplus", "sqlcl", "mysql", "mariadb", "sqlcmd", "sqlite3", "isql", "usql", "pgcli", "mycli"}
SQL_WRITE = re.compile(r"\b(update|delete|insert|drop|truncate|alter|create|grant|revoke|merge|replace|upsert|"
                       r"call|exec|execute|into|vacuum|reindex|copy)\b|\\!|\\copy|\\i\b|\\o\b", re.I)
SQL_READ = re.compile(r"\b(select|show|explain|describe|desc|with|pragma|values)\b|\\d|\\l\b|\\z\b", re.I)
SQL_WHERE = re.compile(r"\bwhere\b", re.I)

SIMPLE_READ = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "pwd", "echo", "printf",
    "date", "whoami", "id", "uname", "hostname", "df", "du", "free", "uptime", "ps", "pgrep", "which", "type",
    "file", "stat", "tree", "sort", "uniq", "cut", "tr", "diff", "cmp", "basename", "dirname", "realpath",
    "readlink", "printenv", "dig", "nslookup", "host", "jq", "column", "nl", "md5sum", "sha1sum", "sha256sum",
    "lsblk", "ss", "netstat", "true", "false", "test", "[", "seq", "tac", "rev", "strings", "zcat", "lsof",
    "[[", "nproc", "hexdump", "od", "xxd", "locate", "cd", "ll", "la", "lscpu", "lsmod", "env", "ip", "getent", "arch",
    "whereis", "xmllint", "yq", "paste", "comm", "fold", "expand", "fmt", "pr", "tty", "groups", "last", "w",
    "who", "lspci", "lsusb", ":", "sleep", "expr", "bc", "numfmt", "tput", "man", "help", "history",
}
SUBCOMMAND_PROGRAMS = {"git", "docker", "kubectl", "systemctl", "npm", "pip", "pip3", "apt", "apt-get", "brew",
                       "cargo", "uv", "make", "gh", "helm", "terraform", "podman", "yarn", "pnpm", "snap", "flatpak"}
SSH_VALUE_FLAGS = set("pilFJLRDbcEeIMmOoQSWw")
FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
GIT_READ = {"status", "log", "diff", "show", "ls-files", "ls-tree", "rev-parse", "describe", "blame", "shortlog",
            "grep", "cat-file", "rev-list", "reflog", "whatchanged", "count-objects", "name-rev", "merge-base",
            "for-each-ref", "show-ref", "diff-tree", "check-ignore", "var", "version", "help", "ls-remote"}
GIT_BRANCH_WRITES = {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--move", "--copy", "--force",
                     "--set-upstream-to", "-u", "--unset-upstream", "--edit-description"}
DOCKER_READ = {"ps", "images", "logs", "inspect", "info", "version", "stats", "top", "port", "history", "diff"}
DOCKER_GROUP_READ = {"ls", "list", "inspect", "ps", "logs", "config", "images", "top", "df", "ps"}
KUBECTL_READ = {"get", "describe", "logs", "top", "version", "api-resources", "api-versions", "explain", "cluster-info"}
SYSTEMCTL_READ = {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "list-timers",
                  "list-sockets", "show", "cat", "list-dependencies"}
NPM_READ = {"ls", "list", "view", "outdated", "root", "info", "audit"}
PIP_READ = {"list", "show", "freeze", "check"}
GH_READ = {"view", "list", "status", "diff", "checks"}
CURL_WRITE_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "-F", "--form",
                    "-T", "--upload-file", "-o", "--output", "-O", "--remote-name", "-K", "--config", "-J",
                    "--json", "--post301", "--post302", "--post303"}


@dataclass(frozen=True)
class Step:
    prefix: str
    op: str
    hard: bool = False

    @property
    def label(self) -> str:
        return f"{self.prefix} · {self.op}"


# ---- tokenizing ------------------------------------------------------------------------------------
_IGNORED_REDIRECT = re.compile(r">>?\s*/dev/null(?![\w/.-])|>&\s*(\d+|-)")


def _match_paren(text: str, start: int) -> int:
    """Index of the ')' closing the '(' at `start`, or -1."""
    depth, i, quote = 0, start, None
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split(text: str):
    """-> (segments, inner_commands, flags) where flags has `redirect`, `heredoc` and `ok`. Never raises."""
    segments, inner, buf = [], [], []
    flags = {"redirect": False, "heredoc": False, "ok": True}
    quote, i, n = None, 0, len(text)

    def flush():
        segment = "".join(buf).strip()
        buf.clear()
        if segment:
            segments.append(segment)

    while i < n:
        c = text[i]
        if c == "\\" and quote != "'":
            buf.append(text[i:i + 2])
            i += 2
            continue
        if quote == "'":
            buf.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "`":
            end = text.find("`", i + 1)
            if end < 0:
                flags["ok"] = False
                break
            inner.append(text[i + 1:end])
            buf.append("SUBST")
            i = end + 1
            continue
        if c == "$" and text[i + 1:i + 2] == "{" and quote != "'":
            end = text.find("}", i)
            if end < 0:
                flags["ok"] = False
                break
            buf.append("VAR")
            i = end + 1
            continue
        if c == "$" and text[i + 1:i + 2] == "(":
            end = _match_paren(text, i + 1)
            if end < 0:
                flags["ok"] = False
                break
            inner.append(text[i + 2:end].lstrip("("))
            buf.append("SUBST")
            i = end + 1
            continue
        if quote == '"':
            buf.append(c)
            if c == '"':
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
            i += 1
            continue
        two = text[i:i + 2]
        if two in ("&&", "||"):
            flush()
            i += 2
            continue
        if c in ";\n|(){}":
            flush()
            i += 1
            continue
        if c == "&":
            previous = text[i - 1:i]
            if previous == ">" or text[i + 1:i + 2] == ">":
                buf.append(c)       # 2>&1 / &>file: handled with the '>' below
            else:
                flush()
            i += 1
            continue
        if c == "<":
            if two == "<<":
                flags["heredoc"] = True
            if text[i + 1:i + 2] == "(":
                flags["redirect"] = True        # <( ) process substitution runs a command
            buf.append(c)
            i += 1
            continue
        if c == ">":
            rest = text[i:]
            ignored = _IGNORED_REDIRECT.match(rest)
            if ignored:
                if buf and buf[-1].isdigit() and (len(buf) == 1 or buf[-2].isspace()):
                    buf.pop()                   # the "2" of 2>/dev/null
                i += ignored.end()
                continue
            flags["redirect"] = True
            buf.append(c)
            i += 1
            continue
        buf.append(c)
        i += 1
    if quote:
        flags["ok"] = False
    flush()
    return segments, inner, flags


def _tokens(segment: str):
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return None
    while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
        tokens.pop(0)
    while tokens and tokens[0] in ("time", "command", "builtin", "nohup", "exec"):
        tokens.pop(0)
    return tokens


# ---- read-only rules per program --------------------------------------------------------------------
def _args_after(args, count=1):
    """The arguments that are not options, after skipping `count` of them."""
    out = [a for a in args if not a.startswith("-")]
    return out[count:]


def _first_arg(args):
    for a in args:
        if not a.startswith("-"):
            return a
    return ""


def _is_read(prog: str, args: list) -> bool:
    if prog in SIMPLE_READ:
        if prog == "sort":
            return not any(a == "-o" or a.startswith("--output") or (a.startswith("-o") and not a.startswith("--"))
                           for a in args)
        if prog == "date":
            return not any(a in ("-s", "--set") or a.startswith("--set=") for a in args)
        if prog == "hostname":
            return not _first_arg(args)
        if prog == "env":
            return not args
        if prog == "ip":
            return _first_arg(args) in ("", "a", "addr", "address", "r", "route", "l", "link", "n", "neigh", "-s")
        if prog == "history":
            return not any(a in ("-c", "-d", "-w", "-a") for a in args)
        if prog == "yq":
            return not any(a.startswith("-i") or a == "--inplace" for a in args)
        return True
    if prog == "sort":
        return False
    if prog == "find":
        return not any(a in FIND_WRITES for a in args)
    if prog == "sed":
        return not any(a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--") and "i" in a)
                       for a in args)
    if prog in ("awk", "gawk", "mawk"):
        text = " ".join(args)
        return not re.search(r"system\s*\(|getline|>|\||-i\s|inplace", text)
    if prog == "git":
        return _git_is_read(args)
    if prog in ("docker", "podman"):
        sub = _first_arg(args)
        rest = _args_after(args)
        return sub in DOCKER_READ or (sub in ("container", "image", "volume", "network", "compose", "system", "context")
                                      and bool(rest) and rest[0] in DOCKER_GROUP_READ)
    if prog == "kubectl":
        sub = _first_arg(args)
        rest = _args_after(args)
        return sub in KUBECTL_READ or (sub == "config" and bool(rest) and rest[0] in ("view", "get-contexts", "current-context"))
    if prog == "systemctl":
        return _first_arg(args) in SYSTEMCTL_READ
    if prog == "journalctl":
        return not any(a.startswith(("--vacuum", "--rotate", "--flush", "--sync", "--relinquish")) for a in args)
    if prog in ("npm", "yarn", "pnpm"):
        return _first_arg(args) in NPM_READ
    if prog in ("pip", "pip3"):
        return _first_arg(args) in PIP_READ
    if prog == "gh":
        sub, rest = _first_arg(args), _args_after(args)
        return sub in ("pr", "issue", "run", "repo", "release", "workflow") and bool(rest) and rest[0] in GH_READ | {"view"}
    if prog == "curl":
        return not any(a in CURL_WRITE_FLAGS or a.startswith(("--data", "--form", "--upload", "--output")) for a in args) \
            and not _curl_changes_method(args)
    return False


def _curl_changes_method(args) -> bool:
    for i, a in enumerate(args):
        value = None
        if a in ("-X", "--request"):
            value = args[i + 1] if i + 1 < len(args) else ""
        elif a.startswith("--request="):
            value = a.split("=", 1)[1]
        elif a.startswith("-X") and len(a) > 2:
            value = a[2:]
        if value is not None and value.upper() not in ("GET", "HEAD"):
            return True
    return False


def _git_is_read(args) -> bool:
    sub = _first_arg(args)
    rest = args[args.index(sub) + 1:] if sub in args else []
    if sub in GIT_READ:
        return True
    positional = [a for a in rest if not a.startswith("-")]
    if sub == "branch":
        return not any(a in GIT_BRANCH_WRITES for a in rest) and (not positional or "--list" in rest or "-l" in rest)
    if sub == "tag":
        return not any(a in ("-d", "-f", "-a", "-s", "-m", "-u", "--delete", "--force") for a in rest) and \
            (not positional or "--list" in rest or "-l" in rest)
    if sub == "remote":
        return not rest or rest[0] in ("-v", "--verbose", "show", "get-url")
    if sub == "config":
        return any(a in ("--get", "--get-all", "--list", "-l", "--get-regexp") for a in rest)
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "worktree":
        return bool(rest) and rest[0] == "list"
    return False


# ---- analysis -------------------------------------------------------------------------------------
def _prefix(prog: str, args: list) -> str:
    if prog in SUBCOMMAND_PROGRAMS:
        sub = _first_arg(args)
        return f"{prog} {sub}".strip()
    return prog


def _ssh_host(args):
    """(host, remote_command_text) of an ssh invocation."""
    i = 0
    host = None
    while i < len(args):
        a = args[i]
        if host is None:
            if a.startswith("-"):
                if len(a) == 2 and a[1] in SSH_VALUE_FLAGS:
                    i += 1
            else:
                host = a
        else:
            return host, " ".join(args[i:])
        i += 1
    return host, ""


def _wrap(prefix: str, inner_steps) -> list:
    """Steps of a command run through `prefix` (ssh host, bash -c): the wrapper's prefix + what the inner command does."""
    return [Step(prefix, s.prefix if s.op == "run" else s.op, s.hard) for s in inner_steps]


def analyze(command: str, _depth: int = 0) -> list:
    """The steps of `command` that are not read-only (empty list = the command only reads)."""
    if _depth > MAX_DEPTH:
        return [Step("(too deeply nested)", "run", True)]
    segments, inner, flags = _split(command)
    steps = []
    if not flags["ok"]:
        return [Step("(command that could not be parsed)", "run", True)]
    if flags["redirect"]:
        steps.append(Step(">", "write to file", True))
    if flags["heredoc"]:
        steps.append(Step("<<", "here-document", True))
    for text in inner:
        steps.extend(analyze(text, _depth + 1))

    parsed = []
    for segment in segments:
        tokens = _tokens(segment)
        if tokens is None:
            return [Step("(command that could not be parsed)", "run", True)]
        if tokens:
            parsed.append(tokens)

    client = next((t for t in parsed if any(os.path.basename(x) in SQL_CLIENTS for x in t)), None)
    if client is not None:
        steps.extend(_sql_steps(command, client))

    for tokens in parsed:
        steps.extend(_segment_steps(tokens, _depth, sql=client is not None))
    unique = []
    for s in steps:
        if s not in unique:
            unique.append(s)
    return unique


def _sql_steps(command: str, client_tokens: list) -> list:
    prog = os.path.basename(client_tokens[0])
    prefix = f"ssh {_ssh_host(client_tokens[1:])[0]}" if prog == "ssh" else _prefix(prog, client_tokens[1:])
    writes = []
    for m in SQL_WRITE.finditer(command):
        word = m.group(0)
        writes.append(word.upper() if word[0].isalpha() else word)
    steps = []
    for op in dict.fromkeys(writes):
        hard = op in ("UPDATE", "DELETE") and not SQL_WHERE.search(command)
        steps.append(Step(prefix, op, hard))
    if not steps and not SQL_READ.search(command) and prog not in ("ssh", "bash", "sh", "zsh", "dash"):
        steps.append(Step(prefix, "run"))       # reads a script or stdin: we cannot see the SQL
    return steps


def _segment_steps(tokens: list, depth: int, sql: bool) -> list:
    prog, args = os.path.basename(tokens[0]), tokens[1:]
    if prog in ("sudo", "doas", "su", "pkexec", "eval"):
        return [Step(prog, "run", True)]
    if prog == "ssh":
        host, remote = _ssh_host(args)
        if not remote:
            return [Step(f"ssh {host or ''}".strip(), "interactive session", True)]
        return _wrap(f"ssh {host}", analyze(remote, depth + 1))
    if prog in ("bash", "sh", "zsh", "dash") and "-c" in args:
        i = args.index("-c")
        if i + 1 < len(args):
            return _wrap(f"{prog} -c", analyze(args[i + 1], depth + 1))
        return [Step(prog, "run")]
    if prog in SQL_CLIENTS:
        return []       # judged as a whole by _sql_steps
    if sql and any(os.path.basename(x) in SQL_CLIENTS for x in tokens):
        return []       # something like `ssh host psql ...`: judged as a whole by _sql_steps
    if _is_read(prog, args):
        return []
    return [Step(_prefix(prog, args), "run")]


# ---- saved rules -----------------------------------------------------------------------------------
def rules_path() -> Path:
    from nina import config
    return config.CONFIG_FILE.parent / "auto-rules.json"


class Rules:
    """The "Allow always" rules, kept in a small JSON file. Safe to use from the UI and the agent threads."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.lock = threading.Lock()
        self.items = []
        self.load()

    def location(self) -> Path:
        return self.path or rules_path()

    def load(self) -> None:
        try:
            data = json.loads(self.location().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        items = []
        for r in data if isinstance(data, list) else []:
            if isinstance(r, dict) and isinstance(r.get("prefix"), str) and isinstance(r.get("op"), str):
                items.append((r["prefix"], r["op"]))
        with self.lock:
            self.items = list(dict.fromkeys(items))

    def save(self) -> None:
        target = self.location()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps([{"prefix": p, "op": o} for p, o in self.items], indent=2, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(target)
        except OSError:
            pass

    def covers(self, step: Step) -> bool:
        with self.lock:
            return not step.hard and (step.prefix, step.op) in self.items

    def add(self, steps) -> None:
        with self.lock:
            for s in steps:
                if not s.hard and (s.prefix, s.op) not in self.items:
                    self.items.append((s.prefix, s.op))
        self.save()

    def remove(self, prefix: str, op: str) -> None:
        with self.lock:
            self.items = [i for i in self.items if i != (prefix, op)]
        self.save()

    def all(self) -> list:
        with self.lock:
            return list(self.items)


class AutoMode:
    """The switch plus the saved rules, shared by every chat. `on_change(enabled)` lets the owner persist the switch."""

    def __init__(self, enabled: bool = False, rules: Rules | None = None, on_change=None):
        self.on = bool(enabled)
        self.rules = rules or Rules()
        self.on_change = on_change

    def enabled(self) -> bool:
        return self.on

    def set_enabled(self, value: bool) -> None:
        self.on = bool(value)
        if self.on_change is not None:
            self.on_change(self.on)

    def pending(self, command: str) -> list:
        """The steps of `command` that still need your answer (empty = it can run now)."""
        return [s for s in analyze(command) if s.hard or not self.rules.covers(s)]
