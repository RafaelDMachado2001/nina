"""
`@path` mentions for the chat: find the mention being typed and suggest files and folders for it.

Without a prefix the search is by name inside the chat's folder. A query that starts with `/`, `~`, `./`, `../` (or a
drive like `C:/` on Windows) completes real paths one level at a time, anywhere on the computer. Suggestions always use
`/`, also on Windows, where Claude Code accepts it too. Pure standard library: no Qt.
"""
import os
import re
import time
from dataclasses import dataclass

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".cache",
                ".tox", ".idea"}
MAX_SCANNED = 20000
MAX_SECONDS = 0.3
PATH_PREFIXES = ("/", "~", "./", "../")
DRIVE = re.compile(r"^[A-Za-z]:[/\\]")


@dataclass(frozen=True)
class Suggestion:
    value: str          # what goes after the "@" (a directory ends with "/")
    is_dir: bool


def active_mention(text: str, cursor: int):
    """(index of the "@", what was typed after it) for the mention under the cursor, or None."""
    before = text[:cursor]
    m = re.search(r'(?:^|\s)@"([^"]*)$', before)
    if m:
        return m.end() - len(m.group(1)) - 2, m.group(1)
    m = re.search(r"(?:^|\s)@(\S*)$", before)
    if m:
        return m.end() - len(m.group(1)) - 1, m.group(1)
    return None


def format_mention(value: str) -> str:
    return f'@"{value}"' if " " in value else f"@{value}"


def expand_home(text: str) -> str:
    """`@~/notes.md` -> `@/home/you/notes.md`, so the agent can read it."""
    home = os.path.expanduser("~").replace(os.sep, "/")
    return re.sub(r'(?<!\S)@(")?~(?=/|"|\s|$)', lambda m: "@" + (m.group(1) or "") + home, text)


def suggest(query: str, cwd: str, limit: int = 10) -> list:
    if query.startswith(PATH_PREFIXES) or query in (".", "..") or DRIVE.match(query):
        return complete_path(query, cwd, limit)
    return search(query, cwd, limit)


def _entries(directory: str):
    try:
        with os.scandir(directory) as it:
            return [(e.name, e.is_dir(follow_symlinks=True)) for e in it]
    except OSError:
        return []


def complete_path(query: str, cwd: str, limit: int = 10) -> list:
    if query == "~":
        return [Suggestion("~/", True)]
    if query in (".", ".."):
        return [Suggestion(query + "/", True)]
    if os.sep == "\\":
        query = query.replace("\\", "/")
    typed_dir, prefix = query[: query.rfind("/") + 1], query[query.rfind("/") + 1:]
    directory = os.path.expanduser(typed_dir)
    if not os.path.isabs(directory):
        directory = os.path.join(cwd, directory)
    shown = [(n, d) for n, d in _entries(directory)
             if n.lower().startswith(prefix.lower()) and (prefix.startswith(".") or not n.startswith("."))]
    shown.sort(key=lambda e: (not e[1], e[0].lower()))
    return [Suggestion(typed_dir + n + ("/" if d else ""), d) for n, d in shown[:limit]]


def _subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(c in it for c in needle)


def search(query: str, cwd: str, limit: int = 10) -> list:
    """Files and folders under `cwd` whose name or path matches `query` (empty query = the top level)."""
    q = query.lower()
    if not q:
        shown = [(n, d) for n, d in _entries(cwd) if not n.startswith(".") and n not in IGNORED_DIRS]
        shown.sort(key=lambda e: (not e[1], e[0].lower()))
        return [Suggestion(n + ("/" if d else ""), d) for n, d in shown[:limit]]
    found, scanned, started = [], 0, time.monotonic()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, cwd)
        for name, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files if not f.startswith(".")]:
            scanned += 1
            rel = name if rel_root == "." else os.path.join(rel_root, name).replace(os.sep, "/")
            low, base = rel.lower(), name.lower()
            if base.startswith(q):
                score = 0
            elif q in base:
                score = 1
            elif q in low:
                score = 2
            elif _subsequence(q, low):
                score = 3
            else:
                continue
            found.append((score, rel.count("/"), len(rel), rel, is_dir))
        if scanned >= MAX_SCANNED or time.monotonic() - started > MAX_SECONDS:
            break
    found.sort()
    return [Suggestion(rel + ("/" if is_dir else ""), is_dir) for *_k, rel, is_dir in found[:limit]]
