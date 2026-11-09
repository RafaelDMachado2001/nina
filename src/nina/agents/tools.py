"""NINA's own tools, for agents on connections that only *ask* for actions (OpenAI-compatible APIs).

Named and shaped like Claude Code's tools, so roles, the permission gate, auto mode and the UI treat every agent the
same way. Every function returns text for the model; failures are "Error: ..." text, never exceptions. Permission is
NOT checked here: the caller runs each call through the gate first.
"""
import asyncio
import fnmatch
import html
import html.parser
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from nina.mentions import IGNORED_DIRS

MAX_RESULT = 30_000
READ_MAX_LINES = 2000
READ_MAX_BYTES = 256 * 1024
MAX_MATCHES = 200
BASH_TIMEOUT_S, BASH_MAX_TIMEOUT_S = 120, 600
FETCH_MAX_BYTES, FETCH_TIMEOUT_S = 1024 * 1024, 20


def _fn(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_S, _I, _B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
SPECS = {
    "Read": _fn("Read", "Read a text file. Returns numbered lines. Use offset/limit for long files.",
                {"file_path": {**_S, "description": "Path, absolute or relative to the working folder"},
                 "offset": {**_I, "description": "First line to read (1-based)"},
                 "limit": {**_I, "description": "How many lines to read"}}, ["file_path"]),
    "Glob": _fn("Glob", "Find files by glob pattern (e.g. **/*.py), newest first.",
                {"pattern": _S, "path": {**_S, "description": "Folder to search (default: the working folder)"}},
                ["pattern"]),
    "Grep": _fn("Grep", "Search file contents with a regular expression.",
                {"pattern": _S, "path": _S, "glob": {**_S, "description": "Only files matching this glob, e.g. *.md"},
                 "case_insensitive": _B,
                 "output_mode": {"type": "string", "enum": ["files_with_matches", "content"]}}, ["pattern"]),
    "WebFetch": _fn("WebFetch", "Fetch a web page (http/https) and return its text.", {"url": _S}, ["url"]),
    "Write": _fn("Write", "Create or overwrite a file with the given content.",
                 {"file_path": _S, "content": _S}, ["file_path", "content"]),
    "Edit": _fn("Edit", "Replace text in a file. old_string must appear exactly once unless replace_all is true.",
                {"file_path": _S, "old_string": _S, "new_string": _S, "replace_all": _B},
                ["file_path", "old_string", "new_string"]),
    "Bash": _fn("Bash", "Run a shell command (bash) in the working folder and return its output and exit code.",
                {"command": _S, "timeout_s": {**_I, "description": f"Seconds (default {BASH_TIMEOUT_S}, max {BASH_MAX_TIMEOUT_S})"}},
                ["command"]),
}


def delegate_spec(targets: list) -> dict:
    """The Delegate tool, limited to the roles this agent may hand work to."""
    return _fn("Delegate", "Hand a self-contained task to another agent (a role) and get its answer back.",
               {"role": {"type": "string", "enum": list(targets), "description": "Which role does the task"},
                "task": {"type": "string", "description": "Everything the other agent needs: it does not see this conversation"}},
               ["role", "task"])


def offered(role: dict, has_bash: bool) -> list:
    """The tools this role gets: never Write/Edit for a read-only role, no Bash without a shell."""
    names = ["Read", "Glob", "Grep", "WebFetch"]
    if role.get("writes"):
        names += ["Write", "Edit"]
    if has_bash:
        names.append("Bash")
    return [SPECS[n] for n in names]


def find_bash() -> str | None:
    """POSIX: bash (or sh). Windows: Git for Windows' bash, the shell Claude Code itself uses there; never the WSL
    launcher in System32, which would run in another system."""
    if sys.platform != "win32":
        return shutil.which("bash") or shutil.which("sh")
    candidates = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent
        candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        if base:
            candidates += [Path(base) / "Git" / "bin" / "bash.exe", Path(base) / "Programs" / "Git" / "bin" / "bash.exe"]
    for c in candidates:
        if c.is_file() and "system32" not in str(c).lower():
            return str(c)
    return None


# ---- helpers ------------------------------------------------------------------------------------------
class ToolError(Exception):
    pass


def _path(cwd: str, raw) -> Path:
    if not raw or not isinstance(raw, str):
        raise ToolError("a path is required")
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else Path(cwd) / p


def _shown(path: Path, cwd: str) -> str:
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return str(path)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_RESULT else text[:MAX_RESULT] + f"\n… [truncated, {len(text) - MAX_RESULT} more characters]"


def _is_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\0" in f.read(8192)


def _ignored(rel_parts) -> bool:
    return any(part in IGNORED_DIRS for part in rel_parts)


# ---- the tools ----------------------------------------------------------------------------------------
def read(cwd, file_path=None, offset=None, limit=None, **_):
    path = _path(cwd, file_path)
    if not path.is_file():
        raise ToolError(f"no such file: {file_path}")
    if _is_binary(path):
        raise ToolError(f"{file_path} is a binary file")
    with open(path, "rb") as f:
        data = f.read(READ_MAX_BYTES + 1)
    text = data[:READ_MAX_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(offset or 1))
    count = max(1, min(int(limit or READ_MAX_LINES), READ_MAX_LINES))
    chosen = lines[start - 1:start - 1 + count]
    out = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(chosen, start))
    if len(data) > READ_MAX_BYTES or (not limit and start - 1 + count < len(lines)):
        out += f"\n… [more lines: use offset={start + len(chosen)}]"
    return out or "(empty file)"


def write(cwd, file_path=None, content=None, **_):
    if content is None:
        raise ToolError("content is required")
    path = _path(cwd, file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8", newline="")
    return f"Wrote {len(str(content))} characters to {_shown(path, cwd)}."


def edit(cwd, file_path=None, old_string=None, new_string=None, replace_all=False, **_):
    if old_string is None or new_string is None:
        raise ToolError("old_string and new_string are required")
    path = _path(cwd, file_path)
    if not path.is_file():
        raise ToolError(f"no such file: {file_path}")
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    count = text.count(old_string) if old_string else 0
    if count == 0:
        raise ToolError("old_string was not found in the file")
    if count > 1 and not replace_all:
        raise ToolError(f"old_string appears {count} times; give more context or set replace_all")
    text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return f"Edited {_shown(path, cwd)} ({count if replace_all else 1} replacement(s))."


def _split_glob(pattern: str):
    """An absolute pattern (C:/x/**/*.py, /home/x/*.md) as (folder, relative pattern): pathlib only globs relatively."""
    p = Path(os.path.expanduser(pattern))
    if not p.is_absolute():
        return None, pattern
    parts = p.parts
    first = next((i for i, part in enumerate(parts) if any(ch in part for ch in "*?[")), len(parts) - 1)
    return Path(*parts[:first]), "/".join(parts[first:])


def glob(cwd, pattern=None, path=None, **_):
    if not pattern:
        raise ToolError("pattern is required")
    folder, pattern = _split_glob(pattern)
    base = folder or (_path(cwd, path) if path else Path(cwd))
    found = []
    for p in base.glob(pattern):
        try:
            rel = p.relative_to(base).parts
        except ValueError:
            rel = p.parts
        if p.is_file() and not _ignored(rel[:-1]):
            found.append(p)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = [_shown(p, cwd) for p in found[:MAX_MATCHES]]
    if len(found) > MAX_MATCHES:
        out.append(f"… [{len(found) - MAX_MATCHES} more]")
    return "\n".join(out) or "No files found."


def grep(cwd, pattern=None, path=None, glob=None, case_insensitive=False, output_mode="files_with_matches", **_):
    if not pattern:
        raise ToolError("pattern is required")
    try:
        rx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as e:
        raise ToolError(f"invalid regular expression: {e}") from e
    base = _path(cwd, path) if path else Path(cwd)
    if not base.exists():
        raise ToolError(f"no such file or folder: {path}")
    files = [base] if base.is_file() else []
    if base.is_dir():
        for root, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            files += [Path(root) / n for n in sorted(names)]
    out, matched = [], 0
    for f in files:
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            if _is_binary(f) or f.stat().st_size > 2 * READ_MAX_BYTES * 8:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = [(n, line) for n, line in enumerate(lines, 1) if rx.search(line)]
        if not hits:
            continue
        matched += 1
        if output_mode == "content":
            out += [f"{_shown(f, cwd)}:{n}:{line[:300]}" for n, line in hits]
        else:
            out.append(_shown(f, cwd))
        if len(out) >= MAX_MATCHES:
            out = out[:MAX_MATCHES] + ["… [more matches]"]
            break
    return "\n".join(out) or "No matches."


class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def webfetch(cwd, url=None, **_):
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise ToolError("only http and https URLs can be fetched")
    req = urllib.request.Request(url, headers={"User-Agent": "NINA (+https://github.com/RafaelDMachado2001/nina)"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            raw = r.read(FETCH_MAX_BYTES)
            ctype = r.headers.get("Content-Type", "")
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"could not fetch {url}: {e}") from e
    text = raw.decode(charset, errors="replace")
    if "html" in ctype or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        parser = _Text()
        parser.feed(text)
        text = html.unescape("".join(parser.parts))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text or "(empty page)"


async def bash(cwd, bash_path, command=None, timeout_s=None, hide_env=(), **_):
    if not bash_path:
        raise ToolError("no bash shell is available on this computer (on Windows, install Git for Windows)")
    if not command or not isinstance(command, str):
        raise ToolError("command is required")
    timeout = max(1, min(int(timeout_s or BASH_TIMEOUT_S), BASH_MAX_TIMEOUT_S))
    extra = {} if sys.platform == "win32" else {"start_new_session": True}     # its own process group: killable as a whole
    hidden = {name.upper() for name in hide_env}                                # API keys of NINA's connections
    extra["env"] = {k: v for k, v in os.environ.items() if k.upper() not in hidden}
    proc = await asyncio.create_subprocess_exec(bash_path, "-c", command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **extra)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        raise ToolError(f"the command timed out after {timeout}s") from None
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    text = (out or b"").decode("utf-8", errors="replace").rstrip()
    return f"{text}\n(exit code {proc.returncode})".lstrip()


async def _kill_tree(proc) -> None:
    """Kill the shell and everything it started (a child such as `sleep` would otherwise keep the pipe open)."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(proc.pid),
                                                          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass


SYNC = {"Read": read, "Write": write, "Edit": edit, "Glob": glob, "Grep": grep, "WebFetch": webfetch}


async def run(name: str, args: dict, cwd: str, bash_path: str | None, hide_env=()) -> str:
    """Run one tool call. Always returns text: no error of a tool ever ends the agent's answer.
    `hide_env`: environment variables a shell command must not see (the API keys of NINA's connections)."""
    if not isinstance(args, dict):
        return "Error: the arguments must be a JSON object"
    args = {k: v for k, v in args.items() if k != "hide_env"}
    try:
        if name == "Bash":
            return _clip(await bash(cwd, bash_path, hide_env=hide_env, **args))
        fn = SYNC.get(name)
        if fn is None:
            return f"Error: unknown tool {name!r}"
        return _clip(await asyncio.to_thread(fn, cwd, **args))
    except ToolError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
