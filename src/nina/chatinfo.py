"""What the chat's info line shows: folder and git branch, clock, model, context, and usage.

Usage adapts to the account. Plans with rolling windows (Pro/Max) report a 5-hour "session" and a weekly share, shown as
percentages with the reset time. Accounts without windows (API key, Enterprise, pay as you go) get the real consumption:
cost and tokens of the chat and of the day. Nothing here invents a limit.
"""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from nina.i18n import _


# ---- folder and branch -------------------------------------------------------------------------------
def git_branch(folder: str) -> str | None:
    """Current branch of the repository that contains `folder` (a short hash when detached), None outside one."""
    here = Path(folder or ".").expanduser()
    try:
        here = here.resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        dot = d / ".git"
        try:
            if dot.is_file():                                  # worktree or submodule: ".git" points to the real dir
                m = re.match(r"gitdir:\s*(.+)", dot.read_text(encoding="utf-8").strip())
                if not m:
                    return None
                dot = Path(m.group(1))
                dot = dot if dot.is_absolute() else d / dot
            if dot.is_dir():
                head = (dot / "HEAD").read_text(encoding="utf-8").strip()
                if head.startswith("ref:"):
                    return head.split("/", 2)[-1] if head.count("/") >= 2 else head[4:].strip()
                return head[:7] or None
        except OSError:
            return None
    return None


# ---- clock ---------------------------------------------------------------------------------------------
def clock_text(dt: datetime) -> tuple[str, str]:
    """('2026-11-05', '3:42 PM'): ISO date and a 12-hour clock, independent of the system locale."""
    return dt.strftime("%Y-%m-%d"), f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def reset_text(ts, now: float | None = None) -> str:
    """'5:00 PM (in 2h 10m)' for a reset given as a Unix timestamp."""
    if not ts:
        return ""
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(ts)
    secs = max(0, int(ts - now))
    h, m = divmod(secs // 60, 60)
    d, h = divmod(h, 24)
    left = f"{d}d {h}h" if d else (f"{h}h {m:02d}m" if h else f"{m}m")
    clock = clock_text(when)[1]
    return clock if d else f"{clock} ({_('in {left}', left=left)})" if secs else clock


# ---- model and numbers ---------------------------------------------------------------------------------
def friendly_model(model: str) -> str:
    """'claude-sonnet-5-5' -> 'Sonnet 5.5'; anything unknown is returned as it came."""
    m = re.fullmatch(r"claude-(opus|sonnet|haiku|fable)-([\d-]+?)(-\d{8})?(\[\w+\])?", model or "")
    if not m:
        return model or ""
    return f"{m.group(1).capitalize()} {m.group(2).replace('-', '.')}{m.group(4) or ''}"


def compact(n: float) -> str:
    n = float(n or 0)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            v = n / size
            return f"{v:.1f}".rstrip("0").rstrip(".") + suffix if v < 100 else f"{v:.0f}{suffix}"
    return f"{n:.0f}"


def money(v: float) -> str:
    v = float(v or 0)
    return f"${v:.2f}" if v >= 0.1 else f"${v:.3f}"


def size_text(n: int) -> str:
    n = float(n or 0)
    for size, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def pct(fraction) -> int:
    return max(0, min(100, round(float(fraction or 0) * 100)))


# ---- usage of the day, shared by every chat ---------------------------------------------------------------
def usage_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "usage.json"


def load_day(path: Path | None = None, today: str | None = None) -> dict:
    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads((path or usage_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("date") != today:
        return {"date": today, "cost": 0.0, "tokens": 0}
    return {"date": today, "cost": float(data.get("cost") or 0), "tokens": int(data.get("tokens") or 0)}


def add_day(cost: float = 0.0, tokens: int = 0, path: Path | None = None) -> dict:
    data = load_day(path)
    data["cost"] += cost
    data["tokens"] += tokens
    target = path or usage_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError:
        pass
    return data


# ---- usage presentation ---------------------------------------------------------------------------------
def windows_of(rate: dict | None) -> dict:
    """{'five_hour': {'utilization', 'resetsAt'}, 'seven_day': ...} from what the CLI reported, {} when none."""
    if not rate:
        return {}
    out = {k: v for k, v in (rate.get("windows") or {}).items() if isinstance(v, dict) and v.get("utilization") is not None}
    if not out and rate.get("utilization") is not None and rate.get("type"):
        out[rate["type"]] = {"utilization": rate["utilization"], "resetsAt": rate.get("resets_at")}
    return out


def severity(fraction) -> str:
    f = float(fraction or 0)
    return "err" if f >= 0.9 else "warn" if f >= 0.75 else "ok"


def compose(level: int, folder: str, branch: str | None, info: dict, rate: dict | None, now: datetime) -> list[tuple[str, str, str]]:
    """Items of the thin line as (key, text, severity). `level` 0 shows everything and each step drops the least
    important item, so a narrow window keeps context and usage and loses the clock first."""
    items = []
    name = Path(folder).name or folder
    if level < 3 and name:
        items.append(("folder", f"{name} ({branch})" if branch and level < 2 else name, "ok"))
    if level < 4 and info.get("model"):
        items.append(("model", friendly_model(info["model"]), "ok"))
    if info.get("ctx_max"):
        f = info["ctx_tokens"] / info["ctx_max"]
        items.append(("ctx", f"{_('ctx')} {pct(f)}%", severity(f)))
    if level < 5:
        w = windows_of(rate)
        if "five_hour" in w:
            f = w["five_hour"]["utilization"]
            items.append(("usage", f"{_('session')} {pct(f)}%", severity(f)))
        elif w:
            key, v = next(iter(w.items()))
            items.append(("usage", f"{key.replace('seven_day', '7d').replace('five_hour', '5h')} {pct(v['utilization'])}%",
                          severity(v["utilization"])))
        elif info.get("cost") or info.get("tokens"):
            items.append(("usage", f"{money(info.get('cost', 0))} · {compact(info.get('tokens', 0))} tok", "ok"))
    if level < 1:
        d, t = clock_text(now)
        items.append(("clock", f"{d} {t}", "ok"))
    return items


MAX_LEVEL = 5


def details(folder: str, branch: str | None, account: str, info: dict, rate: dict | None, day: dict,
            now: datetime, now_ts: float | None = None) -> list[tuple[str, str]]:
    """Every value, as (label, text) rows for the popup and the tooltip."""
    d, t = clock_text(now)
    rows = [(_("Folder"), folder or "-")]
    if branch:
        rows.append((_("Branch"), branch))
    rows += [(_("Date"), d), (_("Time"), t), (_("AI"), account or _("default"))]
    if info.get("model"):
        rows.append((_("Model"), f"{friendly_model(info['model'])} ({info['model']})"))
    if info.get("ctx_max"):
        rows.append((_("Context"), f"{info['ctx_tokens']:,} / {info['ctx_max']:,} tokens ({pct(info['ctx_tokens'] / info['ctx_max'])}%)"))
    w = windows_of(rate)
    labels = {"five_hour": _("Session (5 h)"), "seven_day": _("Week (7 days)"), "seven_day_opus": _("Week, Opus"),
              "seven_day_sonnet": _("Week, Sonnet")}
    for key, v in w.items():
        text = f"{pct(v['utilization'])}%"
        if v.get("resetsAt"):
            text += f" · {_('resets')} {reset_text(v['resetsAt'], now_ts)}"
        rows.append((labels.get(key, key), text))
    if not w:
        rows.append((_("Usage"), _("pay as you go: no session window reported for this account")))
    rows.append((_("This chat"), f"{money(info.get('cost', 0))} · {info.get('tokens', 0):,} {_('new tokens')}"))
    rows.append((_("Today (NINA)"), f"{money(day.get('cost', 0))} · {day.get('tokens', 0):,} {_('new tokens')}"))
    return rows
