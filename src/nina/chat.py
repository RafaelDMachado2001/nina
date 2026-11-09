"""
Talk to NINA: a small chat window next to the mascot, backed by one persistent managed agent (role `general` by default).

The conversation continues between runs: the Claude session id is kept in `chat.json` in the state directory and
resumed on the next message. Everything the agent wants to do beyond reading still goes through the Allow / Deny
dialog, exactly like the other managed agents.
"""
import base64
import json
import html as html_lib
import os
import re
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QTextDocument, QFontMetrics, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from nina import chat_input, chat_theme, chathistory, chatinfo, config, connections, mentions
from nina.history_view import HistoryView
from nina.agents import managed
from nina.agents import roles as roles_mod
from nina.i18n import _

BUSY = ("starting", "working")


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "chat.json"


def load_saved(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("session"), str) and data["session"] else {}


def saved_connection(saved: dict) -> str:
    """The connection a saved session belongs to; sessions saved before connections existed name a Claude account."""
    if saved.get("connection"):
        return str(saved["connection"])
    return connections.account_to_connection(saved.get("account")) or "claude"


def save_saved(data: dict, path: Path | None = None) -> None:
    target = path or state_path()
    try:
        if not data:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError:
        pass


def tab_state_path(tab_id: str) -> Path:
    """Where one chat keeps its Claude session id. The first chat ("main") keeps the old chat.json."""
    return state_path() if tab_id == "main" else state_path().with_name(f"chat-{tab_id}.json")


def tabs_state_path() -> Path:
    return state_path().with_name("chats.json")


def load_tabs(path: Path | None = None) -> dict:
    """The open chats: {"tabs": [{"id", "title"}, ...], "active": id}. Empty when nothing valid was saved."""
    try:
        data = json.loads((path or tabs_state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    tabs, seen = [], set()
    for t in data.get("tabs", []) if isinstance(data, dict) else []:
        tid = t.get("id") if isinstance(t, dict) else None
        if isinstance(tid, str) and re.fullmatch(r"[0-9a-z]{1,16}", tid) and tid not in seen:
            seen.add(tid)
            account = t.get("account")
            conv = t.get("conv")
            conn = t.get("connection") if isinstance(t.get("connection"), str) else None
            account = account if isinstance(account, str) else None
            tabs.append({"id": tid, "title": str(t.get("title") or "")[:60], "account": account,
                         "connection": conn or connections.account_to_connection(account),
                         "conv": conv if isinstance(conv, str) and re.fullmatch(r"[0-9a-z]{1,16}", conv) else None})
    return {"tabs": tabs, "active": data.get("active")} if tabs else {}


def save_tabs(data: dict, path: Path | None = None) -> None:
    target = path or tabs_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError:
        pass


def new_tab_id() -> str:
    return format(int(time.time() * 1000), "x")


def title_from(text: str, limit: int = 24) -> str:
    """A short tab title taken from the first message."""
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


class ChatController(QObject):
    """One persistent conversation. The UI thread talks to it; the agent runs on the Runner's own thread."""
    message = Signal(str, str)        # kind (user|assistant|tool|denied|error|info), text
    state_changed = Signal(str)       # none | starting | working | idle | closed | error
    attention = Signal(str)           # short text for the mascot to say (permission requests)
    permission_asked = Signal(dict)   # {"req", "tool", "detail", "remember"}: the window shows Allow / Deny
    permission_cleared = Signal()
    account_changed = Signal()        # the connection of this chat changed (the name is kept for older callers)
    hook_event = Signal(dict)         # hook-style events of agents NINA runs itself, for the agents panel
    info_changed = Signal()           # model, context, usage or cost changed
    finished = Signal()               # an answer is complete
    _event = Signal(str, str, object)   # from the Runner's thread, delivered on the UI thread
    RATE = {}                         # account -> last rate-limit report: windows belong to the account, not to a chat

    def __init__(self, context, runner_factory=None, path: Path | None = None, parent=None, tab_id: str = "main",
                 auto=None):
        """`context()` returns the same dict as the managed tab, plus an optional "chat_role". `auto` is the shared
        autoapprove.AutoMode (read-only commands run without asking while it is on)."""
        super().__init__(parent)
        self.tab_id = tab_id
        self.auto = auto
        self.connection = None            # None follows the default (the role's binding, then the default connection)
        self.context = context
        self.path = path
        self.runner_factory = runner_factory or (lambda emit, claude_bin: managed.Runner(emit, claude_bin))
        self.runner = None
        self.agent_id = None
        self.status = "none"
        self.role = None
        self.resuming = False
        self.queue = []
        self.current_req = None
        self.last_user = ""
        self.last_images = []
        self.info = self.fresh_info()
        self.session = ""                 # Claude session id of this conversation, kept for the history
        self.cwd = None                   # folder the conversation runs in (None: the default folder)
        self._agent_cost = 0.0
        self._event.connect(self.on_event)

    # ---- API (UI thread) ---------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.status in BUSY

    @staticmethod
    def fresh_info() -> dict:
        return {"model": "", "ctx_tokens": 0, "ctx_max": 0, "cost": 0.0, "tokens": 0}

    def rate(self) -> dict | None:
        return self.RATE.get(self.effective_connection_id())

    def folder(self) -> str:
        return self.cwd or self.context().get("default_dir", "") or ""

    # ---- which AI this chat runs on -------------------------------------------------------------
    @property
    def account(self):
        """Older name: the Claude account slug of an explicit Claude choice (None when following the default)."""
        conn = self.connection or ""
        return conn.split(":", 1)[1] if conn.startswith("claude:") else None

    @account.setter
    def account(self, slug) -> None:
        self.connection = connections.account_to_connection(slug)

    def _accounts(self) -> list:
        """Claude accounts: the listed ones, the active one and any account folder that exists."""
        ctx = self.context()
        return connections.claude_accounts(ctx.get("accounts"), ctx.get("account", ""), ctx.get("accounts_root"),
                                           also=[self.account] if self.account else [])

    def _resolve(self, explicit) -> tuple:
        ctx = self.context()
        role = self.role_for_chat()
        return connections.resolve(ctx.get("cfg") or {}, self._accounts(), explicit=explicit, role_id=role["id"],
                                   role=role, active_account=ctx.get("account", "") or "", sdk=ctx.get("sdk", True))

    def resolved(self) -> tuple:
        """(connection, model, note) for the next message."""
        return self._resolve(self.connection)

    def effective_connection_id(self) -> str:
        return self.resolved()[0]["id"]

    def connection_name(self) -> str:
        return self.resolved()[0]["name"]

    def effective_account(self) -> str:
        """The Claude account this chat runs on ("" for the plain ~/.claude or another kind of connection)."""
        conn = self.resolved()[0]
        return conn.get("account", "") if conn["kind"] == "claude" else ""

    def connection_entries(self) -> list:
        """[(connection id or None for "default", label)] for the chat's connection menu."""
        default = self._resolve(None)[0]
        entries = [(None, _("Default ({name})", name=default["name"]))]
        cfg = self.context().get("cfg") or {}
        return entries + [(c["id"], c["name"]) for c in connections.all_connections(cfg, self._accounts())]

    def set_account(self, slug) -> bool:
        return self.set_connection(connections.account_to_connection(slug))

    def set_connection(self, conn_id) -> bool:
        """Run this chat on another connection (None = the default). Sessions belong to a connection, so the next
        message starts a new session; the messages already on screen stay."""
        if self.busy or conn_id == self.connection:
            return False
        had_conversation = self.agent_id is not None or bool(load_saved(self.path))
        self.connection = conn_id
        if self.agent_id is not None:
            self.runner.stop(self.agent_id)
            self.agent_id = None
            self.drop_permissions()
            self.set_status("none")
        save_saved({}, self.path)
        self.session = ""
        self.info = self.fresh_info()
        self.info_changed.emit()
        if had_conversation:
            self.message.emit("info", _("Now using {name}: the next message starts a new session.",
                                        name=self.connection_name()))
        self.account_changed.emit()
        return True

    def role_for_chat(self) -> dict:
        known = roles_mod.load_roles(self.context().get("roles_dir"))
        return known.get(self.context().get("chat_role") or "general") or known["general"]

    def ask(self, text: str, images=None) -> bool:
        """Send a message, with optional images ({"media_type", "data"}). Returns False when it was not sent
        (nothing to say, or the agent is still answering). `@~/file` is expanded so the agent can read it."""
        text = (text or "").strip()
        images = list(images or [])
        if not (text or images) or self.busy:
            return False
        self.last_user, self.last_images = text, images
        if text:
            self.message.emit("user", text)
        for image in images:
            self.message.emit("image", image["data"])
        body = mentions.expand_home(text)
        message = {"text": body, "images": images} if images else body
        if self.agent_id is not None and self.status == "idle":
            self.set_status("working")
            self.runner.send(self.agent_id, message)
        else:
            self.start(message)
        return True

    def start(self, prompt: str) -> None:
        ctx = self.context()
        cfg = ctx.get("cfg") or {}
        if self.runner is None:
            self.runner = self.runner_factory(self._event.emit, managed.find_claude_bin(ctx.get("claude_bin", "")))
            self.runner.auto = self.auto
        self.runner.deny = list((cfg.get("permissions") or {}).get("deny") or [])
        self.runner.max_steps = int(cfg.get("max_tool_steps") or 40)
        conn, model, note = self.resolved()
        if note:
            self.message.emit("info", note)
        account = conn.get("account", "") if conn["kind"] == "claude" else ""
        config_dir = None
        if account and (Path(ctx["accounts_root"]) / account).is_dir():
            config_dir = str(Path(ctx["accounts_root"]) / account)
        saved = load_saved(self.path)
        if saved_connection(saved) != conn["id"]:          # a session of another connection cannot be resumed here
            saved = {}
        self.resuming = bool(saved)
        # a session only resumes from the folder it ran in; a chat keeps its folder when only the account changes
        cwd = next((c for c in (saved.get("cwd"), self.cwd) if c and Path(c).is_dir()), ctx["default_dir"])
        self.cwd = cwd
        self.session = saved.get("session", "")
        self.role = self.role_for_chat()
        self.set_status("starting")
        self._agent_cost = 0.0                 # the CLI reports cost cumulatively per agent process
        self.agent_id = self.runner.start(self.role, cwd, prompt, account, config_dir,
                                          resume=saved.get("session"), connection=conn, model=model)

    def retry(self) -> bool:
        """Send the last message again (after an error)."""
        return self.ask(self.last_user, self.last_images)

    def interrupt(self) -> None:
        if self.agent_id is not None and self.status == "working":
            self.message.emit("info", _("Interrupting..."))
            self.runner.interrupt(self.agent_id)

    def reset(self) -> None:
        """Forget the conversation: the next message starts a new Claude session."""
        if self.runner is not None and self.agent_id is not None:
            self.runner.stop(self.agent_id)
        self.agent_id = None
        self.drop_permissions()
        save_saved({}, self.path)
        self.session, self.cwd = "", None
        self.info = self.fresh_info()
        self.info_changed.emit()
        self.set_status("none")
        self.message.emit("reset", "")

    def shutdown(self) -> None:
        if self.runner is not None:
            self.runner.shutdown()

    def set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            self.state_changed.emit(status)

    # ---- events from the agent ----------------------------------------------------------------------
    def on_event(self, agent_id: str, kind: str, payload) -> None:
        if agent_id != self.agent_id:
            return
        if kind == "status" and payload in ("working", "idle", "closed"):
            if payload == "closed":
                self.drop_permissions()
            self.set_status(payload)
            if payload == "idle":
                self.resuming = False
                self.finished.emit()
        elif kind == "session":
            self.session = payload
            save_saved({"session": payload, "connection": self.effective_connection_id(),
                        "account": self.effective_account(), "cwd": self.folder()}, self.path)
            self.info_changed.emit()
        elif kind == "text":
            self.message.emit("assistant", payload)
        elif kind == "tool":
            self.message.emit("tool", f"{payload['name']}: {payload['summary']}")
        elif kind == "denied":
            self.message.emit("denied", _("Blocked ({tool}): {why}", tool=payload["tool"], why=payload["why"]))
        elif kind == "model":
            self.info["model"] = str(payload)
            self.info_changed.emit()
        elif kind == "context":
            self.info.update(ctx_tokens=payload["tokens"], ctx_max=payload["max"])
            if payload.get("model"):
                self.info["model"] = payload["model"]
            self.info_changed.emit()
        elif kind == "usage":
            self.info["tokens"] += int(payload)
            chatinfo.add_day(tokens=int(payload))
            self.info_changed.emit()
        elif kind == "ratelimit":
            self.RATE[self.effective_connection_id()] = payload
            self.info_changed.emit()
        elif kind == "result":
            cost = payload.get("cost")
            if isinstance(cost, (int, float)):
                delta = cost - self._agent_cost if cost >= self._agent_cost else cost
                self._agent_cost = cost
                if delta > 0:
                    self.info["cost"] += delta
                    chatinfo.add_day(cost=delta)
                self.info_changed.emit()
            if payload.get("error") or payload.get("errors"):
                self.message.emit("error", "; ".join(payload.get("errors") or [_("error")]))
        elif kind == "error":
            if self.resuming:      # the saved session could not be resumed: start clean next time
                save_saved({}, self.path)
                self.resuming = False
            self.message.emit("error", str(payload))
            self.drop_permissions()
            self.set_status("error")
        elif kind == "permission":
            self.queue.append(payload)
            self.ask_next()
        elif kind == "turn_error":         # an answer failed (quota, key, network...): the agent is still ready
            self.message.emit("error", str(payload))
            self.drop_permissions()
            self.resuming = False
            self.set_status("idle")
        elif kind == "hook":
            self.hook_event.emit(dict(payload))

    def drop_permissions(self) -> None:
        """The agent is gone: its pending requests were denied by the runner, so forget them here too."""
        self.queue.clear()
        if self.current_req is not None:
            self.current_req = None
            self.permission_cleared.emit()

    def ask_next(self) -> None:
        """One request at a time; each one only proceeds with your "Allow"."""
        if self.current_req is not None or not self.queue:
            return
        self.current_req = self.queue.pop(0)
        self.attention.emit(_("I need your permission!"))
        self.permission_asked.emit(self.current_req)

    def answer(self, allow: bool, always: bool = False) -> None:
        """`always` (only with a request that offers it) also saves the auto mode rule, so it stops asking."""
        req, self.current_req = self.current_req, None
        if req is None:
            return
        always = bool(always and allow and req.get("remember"))
        self.runner.resolve(req["req"], allow, "" if allow else _("Denied by the user."), always)
        if always:
            self.message.emit("info", _("Allowed always: {rules}", rules=", ".join(req["remember"])))
        else:
            self.message.emit("info", (_("Allowed") if allow else _("Denied")) + f": {req['tool']}")
        self.permission_cleared.emit()
        self.ask_next()


# ---- the window ---------------------------------------------------------------------------------------

ASSET = Path(__file__).parent / "assets" / "nina.png"


def make_icon(kind: str, color: str, size: int = 16) -> QIcon:
    """Small line icons (Lucide style) painted in `color`, so they follow the theme without SVG files."""
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size * 2 / 24, size * 2 / 24)
    pen = QPen(QColor(color), 2.4 if kind != "shield" else 2)
    pen.setCapStyle(Qt.RoundCap if kind in ("dots", "close", "plus") else Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    if kind == "dots":
        p.setBrush(QColor(color))
        for cy in (5, 12, 19):
            p.drawEllipse(QPointF(12, cy), 1.2, 1.2)
    elif kind == "close":
        p.drawLine(6, 6, 18, 18)
        p.drawLine(18, 6, 6, 18)
    elif kind == "expand":
        p.drawPolyline([QPointF(4, 9), QPointF(4, 4), QPointF(9, 4)])
        p.drawPolyline([QPointF(20, 15), QPointF(20, 20), QPointF(15, 20)])
        p.drawLine(4, 4, 10, 10)
        p.drawLine(20, 20, 14, 14)
    elif kind == "plus":
        p.drawLine(12, 5, 12, 19)
        p.drawLine(5, 12, 19, 12)
    elif kind == "restore":
        p.drawRect(4, 8, 12, 12)
        p.drawPolyline([QPointF(8, 8), QPointF(8, 4), QPointF(20, 4), QPointF(20, 16), QPointF(16, 16)])
    elif kind == "send":
        p.drawLine(12, 19, 12, 5)
        p.drawLine(5, 12, 12, 5)
        p.drawLine(19, 12, 12, 5)
    elif kind == "arrow":
        p.drawLine(5, 12, 19, 12)
        p.drawLine(12, 5, 19, 12)
        p.drawLine(12, 19, 19, 12)
    elif kind == "shield":
        path = QPainterPath()
        path.moveTo(12, 3)
        path.lineTo(20, 6)
        path.lineTo(20, 13)
        path.cubicTo(20, 18, 16.5, 20.5, 12, 22)
        path.cubicTo(7.5, 20.5, 4, 18, 4, 13)
        path.lineTo(4, 6)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(12, 8, 12, 12)
        p.drawLine(12, 16, 12, 16)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


class Square(QWidget):
    """The 8 x 8 status square."""
    def __init__(self, color: str = "#7ed321"):
        super().__init__()
        self.color = color
        self.setFixedSize(8, 8)

    def set_color(self, color: str) -> None:
        self.color = color
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.color))
        p.end()


class ThinkingDots(QWidget):
    """Three 6 x 6 squares, one lit at a time (steps, no easing), plus the elapsed time."""
    def __init__(self):
        super().__init__()
        self.setObjectName("Thinking")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(32)
        self.frame = 0
        self.started = time.monotonic()
        self.lit, self.dim = "#7ed321", "#3b414d"
        self.label = QLabel("")
        self.label.setObjectName("ThinkingLabel")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.addSpacing(6 * 3 + 4 * 2 + 8)
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.advance)
        self.hide()

    def set_colors(self, lit: str, dim: str) -> None:
        self.lit, self.dim = lit, dim
        self.update()

    def start(self) -> None:
        self.started = time.monotonic()
        self.frame = 0
        self.advance(step=False)
        self.timer.start()
        self.show()

    def stop(self) -> None:
        self.timer.stop()
        self.hide()

    def advance(self, step: bool = True) -> None:
        if step:
            self.frame = (self.frame + 1) % 4
        seconds = int(time.monotonic() - self.started)
        self.label.setText(_("thinking... {n} s", n=seconds))
        self.update()

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        p = QPainter(self)
        y = (self.height() - 6) // 2
        for i in range(3):
            p.fillRect(12 + i * 10, y, 6, 6, QColor(self.lit if i == self.frame % 4 else self.dim))
        p.end()


class DragHeader(QWidget):
    """Header bar: dragging it moves the frameless window."""
    def mousePressEvent(self, e) -> None:
        handle = self.window().windowHandle()
        if e.button() == Qt.LeftButton and handle is not None and self.window().size_mode != "maximized":
            handle.startSystemMove()
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.window().toggle_maximized()
        super().mouseDoubleClickEvent(e)


class InfoBar(QLabel):
    """The thin line under the composer. A click opens the details."""
    clicked = Signal()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class ChatTab:
    """One open chat: its controller, the messages shown for it, and the text typed but not sent yet."""
    def __init__(self, controller: "ChatController", title: str = "", conv: str | None = None, entries=None,
                 created: float | None = None):
        self.controller = controller
        self.title = title
        self.conv = conv or chathistory.new_id()      # id of this conversation in the history
        self.created = created or time.time()
        self.dirty = False                    # messages not yet written to the history
        self.lazy = False                     # reopened tab whose messages are still on disk (read when first shown)
        self.save_timer = None
        self.entries = list(entries or [])    # (kind, text), kept to re-render when the theme or the tab changes
        self.draft = ""
        self.images = []                      # chat_input.Attachment: pasted images not sent yet
        self.attention = False                # a background chat needs a permission answer
        self.close_btn = None


class ChatBox(QWidget):
    """The frameless chat window: header, messages (or the empty state), thinking indicator and composer
    (replaced by the permission sheet while the agent waits for you)."""
    MIN_W, MIN_H = 360, 420
    SIZES = {"normal": (400, 520), "large": (680, 800)}   # "normal" is the default; "maximized" fills the screen

    def __init__(self, controller: ChatController, theme: str = "dark", on_theme=None, factory=None, saved=None,
                 tabs_path: Path | None = None, history_dir: Path | None = None, on_connections=None):
        """`factory(tab_id)` builds the controller of a new chat; `saved` is `load_tabs()` of the last run;
        `on_connections()` opens the Connections and agents dialog (a menu entry under the AI chip)."""
        super().__init__()
        self.on_connections = on_connections
        self.controller = controller          # the chat on screen
        self.factory = factory
        self.tabs_path = tabs_path
        self.history_dir = history_dir
        self.in_history = False
        self.theme = theme if theme in chat_theme.THEMES else "dark"
        self.on_theme = on_theme
        first = ChatTab(controller)
        self.setup_save(first)
        self.tabs = [first]
        self.current = first
        self.entries = first.entries
        self.setObjectName("ChatWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle(_("Talk to NINA"))
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.size_mode = "normal"
        self.size_before_max = None            # geometry to go back to when leaving "maximized"
        self.resize(*self.SIZES["normal"])
        self.setMinimumSize(self.MIN_W, self.MIN_H)

        # header
        self.header = DragHeader()
        self.header.setObjectName("Header")
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        avatar = QLabel()
        pm = QPixmap(str(ASSET))
        if not pm.isNull():
            avatar.setPixmap(pm.scaled(22, 21, Qt.IgnoreAspectRatio, Qt.FastTransformation))
        name = QLabel("NINA")
        name.setObjectName("NameLabel")
        self.dot = Square()
        self.status = QLabel("")
        self.status.setObjectName("StatusLabel")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.account_btn = QPushButton()
        self.account_btn.setObjectName("AccountChip")
        self.account_btn.setCursor(Qt.PointingHandCursor)
        self.account_btn.setToolTip(_("Account for this chat"))
        self.account_btn.clicked.connect(self.open_account_menu)
        self.size_btn = self.icon_button(_("Maximize"))
        self.size_btn.clicked.connect(self.toggle_maximized)
        self.menu_btn = self.icon_button(_("Menu"))
        self.menu_btn.clicked.connect(self.open_menu)
        self.close_btn = self.icon_button(_("Close"))
        self.close_btn.clicked.connect(self.hide)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(12, 0, 6, 0)
        hl.setSpacing(8)
        hl.addWidget(avatar)
        hl.addWidget(name)
        hl.addSpacing(-2)
        hl.addWidget(self.dot)
        hl.addWidget(self.status, 1)
        hl.addWidget(self.account_btn)
        hl.addWidget(self.size_btn)
        hl.addWidget(self.menu_btn)
        hl.addWidget(self.close_btn)

        # messages and empty state
        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.document().setDocumentMargin(12)
        self.log.anchorClicked.connect(self.on_link)
        self.empty = QWidget()
        self.empty.setObjectName("EmptyState")
        self.empty.setAttribute(Qt.WA_StyledBackground, True)
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(12, 12, 12, 12)
        el.setSpacing(12)
        el.addStretch(1)
        big = QLabel()
        if not pm.isNull():
            big.setPixmap(pm.scaled(44, 42, Qt.IgnoreAspectRatio, Qt.FastTransformation))
        el.addWidget(big)
        hello = QLabel(_("Hi. I'm NINA."))
        hello.setObjectName("Hello")
        sub = QLabel(_("Tell me what you need. I read, edit and test without making noise."))
        sub.setObjectName("HelloSub")
        sub.setWordWrap(True)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        texts.addWidget(hello)
        texts.addWidget(sub)
        el.addLayout(texts)
        self.suggestions = []
        sl = QVBoxLayout()
        sl.setSpacing(6)
        for text in (_("Explain this repository"), _("Find why the test fails"), _("Review my last commit")):
            b = QPushButton(text)
            b.setObjectName("Suggestion")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, text=text: self.controller.ask(text))
            self.suggestions.append(b)
            sl.addWidget(b)
        el.addLayout(sl)
        self.body = QStackedWidget()
        self.body.addWidget(self.empty)
        self.body.addWidget(self.log)
        self.history = HistoryView(history_dir)
        self.history.resume.connect(self.open_from_history)
        self.history.back.connect(self.hide_history)
        self.history.deleted.connect(lambda _c: None)
        self.body.addWidget(self.history)

        self.thinking = ThinkingDots()

        # composer and permission sheet
        self.composer = QWidget()
        self.composer.setObjectName("Composer")
        self.composer.setAttribute(Qt.WA_StyledBackground, True)
        self.input = chat_input.MentionInput()
        self.input.setPlaceholderText(_("Message NINA... (@ for files)"))
        self.input.cwd = lambda: self.controller.folder() or os.getcwd()
        self.mention_popup = chat_input.SuggestionPopup(self)
        self.input.popup = self.mention_popup
        self.mention_popup.shown.connect(self.place_popup)
        self.input.image_pasted.connect(lambda image: self.attach_images([image]))
        self.input.images_dropped.connect(self.attach_files)
        self.strip = chat_input.AttachmentStrip()
        self.strip.removed.connect(self.remove_image)
        self.setAcceptDrops(True)
        self.input.setFixedHeight(36)
        self.input.returnPressed.connect(self.submit)
        self.stop_btn = QPushButton(_("Interrupt"))
        self.stop_btn.setObjectName("Interrupt")
        self.stop_btn.setFixedHeight(36)
        self.stop_btn.clicked.connect(lambda: self.controller.interrupt())
        self.send_btn = QPushButton()
        self.send_btn.setObjectName("Send")
        self.send_btn.setToolTip(_("Send"))
        self.send_btn.clicked.connect(self.submit)
        outer = QVBoxLayout(self.composer)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        outer.addWidget(self.strip)
        cl = QHBoxLayout()
        cl.setSpacing(8)
        outer.addLayout(cl)
        cl.addWidget(self.input, 1)
        cl.addWidget(self.stop_btn)
        cl.addWidget(self.send_btn)

        self.sheet = QWidget()
        self.sheet.setObjectName("PermissionSheet")
        self.sheet.setAttribute(Qt.WA_StyledBackground, True)
        self.perm_icon = QLabel()
        self.perm_title = QLabel("")
        self.perm_title.setObjectName("PermTitle")
        self.perm_command = QLabel("")
        self.perm_command.setObjectName("PermCommand")
        self.perm_command.setWordWrap(True)
        self.perm_command.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.perm_where = QLabel("")
        self.perm_where.setObjectName("PermWhere")
        self.deny_btn = QPushButton(_("Deny"))
        self.deny_btn.setObjectName("Deny")
        self.deny_btn.setFixedHeight(36)
        self.deny_btn.clicked.connect(lambda: self.controller.answer(False))
        self.allow_btn = QPushButton(_("Allow"))
        self.allow_btn.setObjectName("Allow")
        self.allow_btn.setFixedHeight(36)
        self.allow_btn.setToolTip("Alt+P")
        self.allow_btn.clicked.connect(lambda: self.controller.answer(True))
        self.always_btn = QPushButton(_("Allow always"))
        self.always_btn.setObjectName("Allow")
        self.always_btn.setFixedHeight(36)
        self.always_btn.clicked.connect(lambda: self.controller.answer(True, always=True))
        self.perm_remember = QLabel("")
        self.perm_remember.setObjectName("PermWhere")
        self.perm_remember.setWordWrap(True)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(self.perm_icon)
        title_row.addWidget(self.perm_title, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.deny_btn)
        buttons.addWidget(self.allow_btn)
        buttons.addWidget(self.always_btn)
        buttons.addStretch(1)
        sheet_layout = QVBoxLayout(self.sheet)
        sheet_layout.setContentsMargins(12, 12, 12, 12)
        sheet_layout.setSpacing(8)
        sheet_layout.addLayout(title_row)
        sheet_layout.addWidget(self.perm_command)
        sheet_layout.addWidget(self.perm_where)
        sheet_layout.addWidget(self.perm_remember)
        sheet_layout.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.tabbar = QTabBar()
        self.tabbar.setExpanding(False)
        self.tabbar.setDrawBase(False)
        self.tabbar.setUsesScrollButtons(True)
        self.tabbar.setElideMode(Qt.ElideRight)
        self.tabbar.setFocusPolicy(Qt.NoFocus)
        self.tabbar.setIconSize(QSize(8, 8))
        self.tabbar.currentChanged.connect(self.on_tab_changed)
        self.tabbar.tabBarClicked.connect(lambda _i: self.hide_history())
        self.add_btn = self.icon_button(_("New chat"))
        self.add_btn.clicked.connect(self.new_tab)
        self.tabrow = QWidget()
        self.tabrow.setObjectName("TabRow")
        self.tabrow.setAttribute(Qt.WA_StyledBackground, True)
        tl = QHBoxLayout(self.tabrow)
        tl.setContentsMargins(4, 0, 4, 0)
        tl.setSpacing(4)
        tl.addWidget(self.tabbar, 1)
        tl.addWidget(self.add_btn)
        self.tabrow.hide()                    # only shown once there are two chats

        layout.addWidget(self.header)
        layout.addWidget(self.tabrow)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.thinking)
        layout.addWidget(self.composer)
        layout.addWidget(self.sheet)
        self.infobar = InfoBar()
        self.infobar.setObjectName("InfoBar")
        self.infobar.setTextFormat(Qt.RichText)
        self.infobar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.infobar.setCursor(Qt.PointingHandCursor)
        self.infobar.setToolTip(_("Click for details"))
        self.infobar.clicked.connect(self.show_details)
        layout.addWidget(self.infobar)
        self.popup = None
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_infobar)
        self.clock_timer.start(20000)
        self.sheet.hide()

        self.grip = QSizeGrip(self)                  # resize from the bottom-right corner
        self.grip.setFixedSize(14, 14)
        self.grip.setStyleSheet("background: transparent;")
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new_tab)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self.close_current)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self.show_history)
        QShortcut(QKeySequence("Alt+P"), self, activated=self.allow_shortcut)
        for keys, step in (("Ctrl+PgDown", 1), ("Ctrl+Tab", 1), ("Ctrl+PgUp", -1), ("Ctrl+Shift+Tab", -1)):
            QShortcut(QKeySequence(keys), self, activated=lambda step=step: self.step_tab(step))

        self.attach(first)
        self.tabbar.blockSignals(True)
        self.tabbar.addTab("")
        self.tabbar.blockSignals(False)
        self.decorate(first)
        self.apply_theme(self.theme)
        self.restore(saved or {})

    # ---- chats (tabs) ------------------------------------------------------------------------------
    def attach(self, tab: ChatTab) -> None:
        """Listen to one chat for as long as it is open, also while another one is on screen."""
        c = tab.controller
        c.message.connect(lambda kind, text, tab=tab: self.on_message(kind, text, tab))
        c.state_changed.connect(lambda _s, tab=tab: self.on_tab_state(tab))
        c.permission_asked.connect(lambda req, tab=tab: self.on_permission(req, tab))
        c.permission_cleared.connect(lambda tab=tab: self.on_tab_state(tab))
        c.account_changed.connect(lambda tab=tab: self.on_account_changed(tab))
        c.info_changed.connect(lambda tab=tab: self.on_info_changed(tab))

    def restore(self, saved: dict) -> None:
        """Reopen the chats of the last run (empty on screen, but each one resumes its Claude session)."""
        entries = saved.get("tabs") or []
        if not entries or entries[0]["id"] != getattr(self.controller, "tab_id", "main") or self.factory is None:
            return
        self.revive(self.tabs[0], entries[0])
        for e in entries[1:]:
            self.revive(self.add_tab(self.factory(e["id"]), e["title"], select=False), e)
        by_id = {getattr(t.controller, "tab_id", ""): i for i, t in enumerate(self.tabs)}
        self.decorate_all()
        self.tabbar.setCurrentIndex(by_id.get(saved.get("active"), 0))

    def revive(self, tab: ChatTab, saved: dict) -> None:
        """Give a reopened tab back its title, account and the messages kept in the history."""
        tab.title = saved["title"]
        tab.controller.connection = saved.get("connection") or connections.account_to_connection(saved.get("account"))
        meta = chathistory.load_meta(saved.get("conv") or "", self.history_dir)
        if meta is not None:
            tab.conv, tab.created = meta["id"], meta["created"] or tab.created
            tab.title = tab.title or meta["title"]
            tab.controller.session = meta["session"]
            tab.controller.cwd = meta["cwd"] or None
            tab.controller.info.update(cost=meta["cost"], tokens=meta["tokens"])
            tab.lazy = True                    # the messages are read only when this tab is opened
        elif saved.get("conv"):
            tab.conv = saved["conv"]

    def ensure_loaded(self, tab: ChatTab) -> None:
        if tab.lazy:
            tab.lazy = False
            rec = chathistory.load(tab.conv, self.history_dir)
            if rec is not None:
                tab.entries[:0] = [tuple(e) for e in rec["entries"]]

    def add_tab(self, controller: ChatController, title: str = "", select: bool = True, conv: str | None = None,
                entries=None, created: float | None = None) -> ChatTab:
        tab = ChatTab(controller, title, conv, entries, created)
        self.setup_save(tab)
        self.tabs.append(tab)
        self.attach(tab)
        self.tabbar.blockSignals(True)
        self.tabbar.addTab("")
        self.tabbar.blockSignals(False)
        self.decorate(tab)
        self.tabrow.setVisible(len(self.tabs) > 1)
        if select:
            self.tabbar.setCurrentIndex(len(self.tabs) - 1)
        self.save_tab_list()
        return tab

    def new_tab(self) -> None:
        if self.factory is not None:
            self.add_tab(self.factory(new_tab_id()))
            self.input.setFocus()

    def close_tab(self, tab: ChatTab) -> None:
        """Close a chat: it stays in the history (you can resume it), its agent stops. The last one is only cleared."""
        c = tab.controller
        self.save_now(tab)
        c.reset()
        if len(self.tabs) == 1:
            return
        i = self.tabs.index(tab)
        c.shutdown()
        was_current = tab is self.current
        self.tabs.pop(i)
        self.tabbar.blockSignals(True)
        self.tabbar.removeTab(i)
        self.tabbar.blockSignals(False)
        self.tabrow.setVisible(len(self.tabs) > 1)
        self.decorate_all()
        if was_current:
            self.select(self.tabs[min(i, len(self.tabs) - 1)])
        self.save_tab_list()

    def close_current(self) -> None:
        self.close_tab(self.current)

    def step_tab(self, step: int) -> None:
        if len(self.tabs) > 1:
            self.tabbar.setCurrentIndex((self.tabbar.currentIndex() + step) % len(self.tabs))

    def on_tab_changed(self, i: int) -> None:
        if 0 <= i < len(self.tabs):
            self.select(self.tabs[i])

    def select(self, tab: ChatTab) -> None:
        """Bring a chat on screen: its messages, draft, permission request and state."""
        if tab is not self.current and self.current in self.tabs:
            self.current.draft = self.input.text()
        self.current = tab
        self.controller = tab.controller
        self.entries = tab.entries
        tab.attention = False
        i = self.tabs.index(tab)
        if self.tabbar.currentIndex() != i:
            self.tabbar.blockSignals(True)
            self.tabbar.setCurrentIndex(i)
            self.tabbar.blockSignals(False)
        self.in_history = False
        self.ensure_loaded(tab)
        self.input.setText(tab.draft)
        self.strip.set_attachments(tab.images)
        self.render_all()
        self.body.setCurrentWidget(self.log if tab.entries else self.empty)
        if self.controller.current_req is not None:
            self.fill_permission(self.controller.current_req)
        self.decorate_all()
        self.refresh_state()
        self.save_tab_list()

    def decorate_all(self) -> None:
        for tab in self.tabs:
            self.decorate(tab)

    def decorate(self, tab: ChatTab) -> None:
        """Tab text, the small state square on its left and its close button."""
        if tab not in self.tabs or len(self.tabs) != self.tabbar.count():
            return
        i = self.tabs.index(tab)
        t = chat_theme.tokens(self.theme)
        c = tab.controller
        if c.current_req is not None or tab.attention:
            color = t["warn"]
        elif c.status == "error":
            color = t["err_text"]
        elif c.busy:
            color = t["indigo"]
        else:
            color = None
        pm = QPixmap(8, 8)
        pm.fill(QColor(color) if color else Qt.transparent)
        self.tabbar.setTabIcon(i, QIcon(pm))
        self.tabbar.setTabText(i, tab.title or _("New chat"))
        self.tabbar.setTabToolTip(i, tab.title or _("New chat"))
        if tab.close_btn is None:
            tab.close_btn = self.icon_button(_("Close chat"))
            tab.close_btn.setObjectName("TabClose")
            tab.close_btn.clicked.connect(lambda _=False, tab=tab: self.close_tab(tab))
        tab.close_btn.setIcon(make_icon("close", t["text2"], 12))
        tab.close_btn.setIconSize(QSize(12, 12))
        if self.tabbar.tabButton(i, QTabBar.RightSide) is not tab.close_btn:
            self.tabbar.setTabButton(i, QTabBar.RightSide, tab.close_btn)
        tab.close_btn.show()

    def on_tab_state(self, tab: ChatTab) -> None:
        self.decorate(tab)
        if tab is self.current:
            self.refresh_state()

    def save_tab_list(self) -> None:
        if self.factory is None:
            return
        save_tabs({"tabs": [{"id": getattr(t.controller, "tab_id", "main"), "title": t.title, "account": t.controller.account,
                             "connection": t.controller.connection, "conv": t.conv} for t in self.tabs],
                   "active": getattr(self.controller, "tab_id", "main")}, self.tabs_path)

    # ---- theme -----------------------------------------------------------------------------------
    def icon_button(self, tip: str) -> QPushButton:
        b = QPushButton()
        b.setObjectName("IconButton")
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def apply_theme(self, name: str) -> None:
        self.theme = name if name in chat_theme.THEMES else "dark"
        t = chat_theme.tokens(self.theme)
        self.setStyleSheet(chat_theme.qss(self.theme))
        self.menu_btn.setIcon(make_icon("dots", t["text2"]))
        self.close_btn.setIcon(make_icon("close", t["text2"]))
        self.send_btn.setIcon(make_icon("send", t["on_accent"], 18))
        self.add_btn.setIcon(make_icon("plus", t["text2"]))
        self.decorate_all()
        self.update_size_button()
        self.send_btn.setIconSize(QSize(18, 18))
        self.perm_icon.setPixmap(make_icon("shield", t["warn"]).pixmap(16, 16))
        for b in self.suggestions:
            b.setIcon(QIcon())
        self.thinking.set_colors(t["accent"], t["border_strong"])
        self.render_all()
        self.refresh_state()

    def set_theme(self, name: str) -> None:
        self.apply_theme(name)
        if self.on_theme is not None:
            self.on_theme(self.theme)

    def open_menu(self) -> None:
        menu = QMenu(self)
        new = menu.addAction(_("New chat"))
        new.setShortcut(QKeySequence("Ctrl+N"))
        new.triggered.connect(self.new_tab)
        new.setEnabled(self.factory is not None)
        hist = menu.addAction(_("History"))
        hist.setShortcut(QKeySequence("Ctrl+H"))
        hist.triggered.connect(self.show_history)
        close = menu.addAction(_("Close chat"))
        close.setShortcut(QKeySequence("Ctrl+W"))
        close.triggered.connect(self.close_current)
        menu.addSeparator()
        for mode, label in (("normal", _("Normal size")), ("large", _("Large size")), ("maximized", _("Maximize"))):
            menu.addAction(label).triggered.connect(lambda _c=False, m=mode: self.set_size_mode(m))
        auto = self.controller.auto
        if auto is not None:
            menu.addSeparator()
            toggle = menu.addAction(_("Auto mode: read commands without asking"))
            toggle.setCheckable(True)
            toggle.setChecked(auto.enabled())
            toggle.triggered.connect(lambda checked: self.set_auto(checked))
            saved = auto.rules.all()
            forget = menu.addMenu(_("Forget a saved rule"))
            if not saved:
                forget.addAction(_("No saved rules")).setEnabled(False)
            for prefix, op in saved:
                forget.addAction(f"{prefix} · {op}").triggered.connect(
                    lambda _c=False, prefix=prefix, op=op: auto.rules.remove(prefix, op))
        menu.addSeparator()
        other = "light" if self.theme == "dark" else "dark"
        menu.addAction(_("Light theme") if other == "light" else _("Dark theme")).triggered.connect(
            lambda: self.set_theme(other))
        menu.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))

    def set_auto(self, enabled: bool) -> None:
        self.controller.auto.set_enabled(enabled)
        self.refresh_state()

    # ---- messages ----------------------------------------------------------------------------------
    def on_message(self, kind: str, text: str, tab: ChatTab | None = None) -> None:
        tab = tab or self.current
        shown = tab is self.current
        if kind == "reset":
            tab.lazy = False
            tab.entries.clear()
            tab.title = ""
            tab.dirty = False
            tab.save_timer.stop()
            tab.conv, tab.created = chathistory.new_id(), time.time()      # whatever comes next is a new conversation
            self.decorate(tab)
            self.save_tab_list()
            if shown:
                self.log.clear()
                if not self.in_history:
                    self.body.setCurrentWidget(self.empty)
            return
        if kind == "image":
            text = chat_input.thumbnail_b64(text)        # keep a small copy: the full image went to the agent
        self.ensure_loaded(tab)
        tab.entries.append((kind, text))
        self.schedule_save(tab)
        if kind in ("user", "image") and not tab.title:
            tab.title = title_from(text) if kind == "user" else _("Image")
            self.decorate(tab)
            self.save_tab_list()
        if shown:
            if not self.in_history:
                self.body.setCurrentWidget(self.log)
            self.append(kind, text)

    def html_for(self, kind: str, text: str) -> str:
        th = self.theme
        if kind == "image":
            name = f"nina-image:{abs(hash(text))}"
            self.log.document().addResource(QTextDocument.ImageResource, QUrl(name),
                                            QImage.fromData(base64.b64decode(text)))
            return chat_theme.image_html(name, th)
        if kind == "user":
            return chat_theme.user_html(text, th)
        if kind == "assistant":
            return chat_theme.assistant_html(text, th)
        if kind == "tool":
            return chat_theme.tool_html(text, th)
        if kind == "denied":
            return chat_theme.notice_html("blocked", text, th)
        if kind == "error":
            return chat_theme.notice_html("error", _("Error: {text}", text=text), th, link=(_("Try again"), "nina:retry"))
        return chat_theme.info_html(text, th)

    def append(self, kind: str, text: str) -> None:
        self.log.append(self.html_for(kind, text))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def render_all(self) -> None:
        self.log.clear()
        for kind, text in self.entries:
            self.log.append(self.html_for(kind, text))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def on_link(self, url: QUrl) -> None:
        if url.scheme() == "nina" and url.path() == "retry":
            self.controller.retry()
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    # ---- state ---------------------------------------------------------------------------------------
    def status_key(self) -> str:
        if self.controller.current_req is not None:
            return "perm"
        return {"starting": "thinking", "working": "thinking", "error": "error"}.get(self.controller.status, "ready")

    def refresh_state(self) -> None:
        t = chat_theme.tokens(self.theme)
        key = self.status_key()
        auto = self.controller.auto is not None and self.controller.auto.enabled()
        self.status.setText({"ready": _("ready"), "thinking": _("thinking"), "perm": _("needs permission"),
                             "error": _("error")}[key] + (" · " + _("auto") if auto else ""))
        self.dot.set_color({"ready": t["accent"], "thinking": t["indigo"], "perm": t["warn"], "error": t["err_text"]}[key])
        busy = self.controller.busy
        asking = self.controller.current_req is not None
        self.update_account_button()
        self.update_infobar()
        self.input.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setVisible(self.controller.status == "working")
        if busy and not asking:
            if not self.thinking.isVisible():
                self.thinking.start()
        else:
            self.thinking.stop()
        self.composer.setVisible(not asking and not self.in_history)
        self.sheet.setVisible(asking)
        if asking:
            self.deny_btn.setFocus()
        elif not busy:
            self.input.setFocus()
        else:
            self.log.setFocus()            # keeps the focus ring off the header buttons while it works

    def on_permission(self, req: dict, tab: ChatTab | None = None) -> None:
        tab = tab or self.current
        if tab is not self.current:
            tab.attention = True                 # shown as a warning square on its tab until you open it
            self.decorate(tab)
        else:
            self.fill_permission(req)
        if not self.isVisible():
            self.show()
            self.raise_()

    def fill_permission(self, req: dict) -> None:
        self.perm_title.setText(_("NINA wants to use {tool}", tool=req["tool"]))
        self.perm_command.setText(str(req.get("detail", ""))[:900])
        where = (self.controller.context() or {}).get("default_dir", "")
        self.perm_where.setText(_("in {where}", where=config.short_path(where)) if where else "")
        remember = req.get("remember") or []
        self.always_btn.setVisible(bool(remember))
        self.perm_remember.setVisible(bool(remember))
        self.perm_remember.setText(_("Allow always saves: {rules}", rules=", ".join(remember)) if remember else "")
        self.refresh_state()

    def allow_shortcut(self) -> None:
        if self.controller.current_req is not None:
            self.controller.answer(True)

    def submit(self) -> None:
        tab = self.current
        if self.controller.ask(self.input.text(), [a.payload for a in tab.images]):
            self.input.clear()
            tab.images = []
            self.strip.set_attachments([])

    # ---- attachments and mentions -------------------------------------------------------------------
    def place_popup(self) -> None:
        chat_input.place_popup(self.mention_popup, self, self.composer)

    def attach_images(self, images: list) -> None:
        tab = self.current
        for image in images:
            if len(tab.images) >= chat_input.MAX_IMAGES:
                self.on_message("info", _("At most {n} images per message.", n=chat_input.MAX_IMAGES), tab)
                break
            attachment = chat_input.make_attachment(image)
            if attachment is not None:
                tab.images.append(attachment)
        self.strip.set_attachments(tab.images)

    def attach_files(self, paths: list) -> None:
        self.attach_images([QImage(p) for p in paths])

    def remove_image(self, index: int) -> None:
        tab = self.current
        if 0 <= index < len(tab.images):
            del tab.images[index]
            self.strip.set_attachments(tab.images)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        """Image files become attachments; any other file or folder becomes an @mention in the field."""
        mime = e.mimeData()
        cwd = os.path.abspath(self.controller.folder() or os.getcwd())
        images, mention = [], []
        for url in mime.urls():
            path = url.toLocalFile()
            if not path:
                continue
            if chat_input.is_image_file(path):
                images.append(path)
            else:
                try:
                    inside = os.path.commonpath([os.path.abspath(path), cwd]) == cwd
                except ValueError:          # Windows: another drive
                    inside = False
                shown = (os.path.relpath(path, cwd) if inside else path).replace(os.sep, "/")
                mention.append(mentions.format_mention(shown + ("/" if os.path.isdir(path) else "")))
        if mime.hasImage() and not mime.hasUrls():
            self.attach_images([QImage(mime.imageData())])
        self.attach_files(images)
        if mention:
            self.input.insert(" ".join(mention) + " ")
            self.input.setFocus()
        e.acceptProposedAction()

    # ---- size ----------------------------------------------------------------------------------------
    def available_rect(self):
        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        return screen.availableGeometry()

    # ---- history ---------------------------------------------------------------------------------------
    def setup_save(self, tab: ChatTab) -> None:
        tab.save_timer = QTimer(self)
        tab.save_timer.setSingleShot(True)
        tab.save_timer.timeout.connect(lambda tab=tab: self.save_now(tab))

    def snapshot(self, tab: ChatTab) -> dict:
        c = tab.controller
        return {"id": tab.conv, "title": tab.title, "account": c.effective_account(),
                "connection": c.effective_connection_id(), "cwd": c.folder(),
                "session": c.session, "created": tab.created, "updated": time.time(), "cost": c.info["cost"],
                "tokens": c.info["tokens"], "entries": tab.entries}

    def schedule_save(self, tab: ChatTab) -> None:
        """Write the conversation to the history shortly after it changes (messages come in bursts)."""
        if tab.entries:
            tab.dirty = True
            tab.save_timer.start(800)

    def save_now(self, tab: ChatTab) -> None:
        tab.save_timer.stop()
        if tab.entries:
            chathistory.save(self.snapshot(tab), self.history_dir)
        tab.dirty = False

    def flush(self) -> None:
        for tab in self.tabs:
            if tab.dirty:
                self.save_now(tab)

    def show_history(self) -> None:
        for tab in self.tabs:
            if tab.dirty:
                self.save_now(tab)                # the list must include what was just said
        self.in_history = True
        self.history.refresh({t.conv for t in self.tabs}, chat_theme.tokens(self.theme))
        self.body.setCurrentWidget(self.history)
        self.refresh_state()

    def hide_history(self) -> None:
        if not self.in_history:
            return
        self.in_history = False
        self.body.setCurrentWidget(self.log if self.current.entries else self.empty)
        self.refresh_state()

    def open_from_history(self, meta: dict) -> None:
        """Open a saved conversation in a tab, with its messages, account and folder, ready to carry on. This is the
        moment its messages are read from disk."""
        for tab in self.tabs:
            if tab.conv == meta["id"]:
                self.select(tab)                  # already open: just go there
                return
        rec = chathistory.load(meta["id"], self.history_dir)
        if self.factory is None or rec is None:
            return
        controller = self.factory(new_tab_id())
        controller.connection = rec.get("connection") or connections.account_to_connection(rec["account"])
        controller.cwd = rec["cwd"] or None
        controller.session = rec["session"]
        controller.info.update(cost=rec["cost"], tokens=rec["tokens"])
        if rec["session"]:
            save_saved({"session": rec["session"], "account": rec["account"],
                        "connection": controller.connection or "", "cwd": rec["cwd"]}, controller.path)
        self.add_tab(controller, rec["title"], conv=rec["id"], entries=[tuple(e) for e in rec["entries"]],
                     created=rec["created"])

    # ---- info line -----------------------------------------------------------------------------------
    def info_context(self):
        c = self.controller
        folder = c.folder()
        return folder, chatinfo.git_branch(folder), c.info, c.rate()

    def update_infobar(self) -> None:
        if not self.isVisible():
            return
        t = chat_theme.tokens(self.theme)
        folder, branch, info, rate = self.info_context()
        now = datetime.now()
        fm = QFontMetrics(self.infobar.font())
        avail = max(40, self.infobar.width() - 24)
        items = []
        for level in range(chatinfo.MAX_LEVEL + 1):
            items = chatinfo.compose(level, folder, branch, info, rate, now)
            if fm.horizontalAdvance(" · ".join(text for _k, text, _s in items)) <= avail:
                break
        colors = {"ok": t["text2"], "warn": t["warn"], "err": t["err_text"]}
        sep = f' <span style="color:{t["border_strong"]}">·</span> '
        self.infobar.setText(sep.join(f'<span style="color:{colors[sev]}">{html_lib.escape(text)}</span>'
                                      for _k, text, sev in items))

    def details_html(self) -> str:
        t = chat_theme.tokens(self.theme)
        folder, branch, info, rate = self.info_context()
        rows = chatinfo.details(config.short_path(folder) if folder else "", branch, self.controller.connection_name(),
                                info, rate, chatinfo.load_day(), datetime.now())
        body = "".join(f'<tr><td style="color:{t["text2"]}; padding-right:12px">{html_lib.escape(k)}</td>'
                       f'<td>{html_lib.escape(v)}</td></tr>' for k, v in rows)
        return f'<table cellspacing="3">{body}</table>'

    def show_details(self) -> None:
        if self.popup is not None:
            self.popup.close()
        pop = QFrame(self, Qt.Popup)
        pop.setObjectName("InfoPopup")
        label = QLabel(self.details_html())
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay = QVBoxLayout(pop)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(label)
        pop.setFixedWidth(min(self.width() - 16, 380))
        pop.adjustSize()
        top_left = self.infobar.mapToGlobal(QPoint(8, 0))
        pop.move(top_left.x(), top_left.y() - pop.height() - 4)
        self.popup = pop
        pop.show()

    # ---- account of this chat ------------------------------------------------------------------------
    def update_account_button(self) -> None:
        conn = self.controller.resolved()[0]
        name = (conn.get("account") or _("default")) if conn["kind"] == "claude" else conn["name"]
        self.account_btn.setText(name if len(name) <= 14 else name[:13] + "…")
        self.account_btn.setToolTip(_("AI for this chat") + f": {conn['name']}")
        self.account_btn.setEnabled(not self.controller.busy)

    def on_info_changed(self, tab: ChatTab) -> None:
        self.schedule_save(tab)
        if tab is self.current:
            self.update_infobar()

    def on_account_changed(self, tab: ChatTab) -> None:
        self.schedule_save(tab)
        self.save_tab_list()
        if tab is self.current:
            self.update_account_button()

    def open_account_menu(self) -> None:
        """The AIs this chat can run on: the default, every Claude account and every connection you added."""
        c = self.controller
        menu = QMenu(self)
        entries = c.connection_entries()
        for i, (conn_id, label) in enumerate(entries):
            if i == 1:
                menu.addSeparator()
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(c.connection == conn_id)
            act.triggered.connect(lambda _c=False, conn_id=conn_id: self.controller.set_connection(conn_id))
        if self.on_connections is not None:
            menu.addSeparator()
            menu.addAction(_("Connections and agents..."), self.on_connections)
        menu.exec(self.account_btn.mapToGlobal(QPoint(0, self.account_btn.height())))

    def update_size_button(self) -> None:
        maximized = self.size_mode == "maximized"
        t = chat_theme.tokens(self.theme)
        self.size_btn.setIcon(make_icon("restore" if maximized else "expand", t["text2"]))
        self.size_btn.setToolTip(_("Restore size") if maximized else _("Maximize"))
        self.grip.setVisible(not maximized)

    def toggle_maximized(self) -> None:
        self.set_size_mode("normal" if self.size_mode == "maximized" else "maximized")

    def set_size_mode(self, mode: str) -> None:
        """normal (default, quick chats), large, or maximized (fills the usable screen area)."""
        screen = self.available_rect()
        if mode == "maximized":
            if self.size_mode != "maximized":
                self.size_before_max = self.geometry()
            self.setGeometry(screen)
        else:
            w, h = self.SIZES[mode]
            w, h = min(w, screen.width() - 16), min(h, screen.height() - 16)
            base = self.size_before_max if self.size_mode == "maximized" and self.size_before_max else self.geometry()
            x = min(max(base.right() + 1 - w, screen.left() + 8), screen.right() - w - 8)   # keep the bottom-right corner,
            y = min(max(base.bottom() + 1 - h, screen.top() + 8), screen.bottom() - h - 8)  # so it grows upward and left
            self.setGeometry(x, y, w, h)
        self.size_mode = mode
        self.update_size_button()

    # ---- placement and keys --------------------------------------------------------------------------
    def show_near(self, anchor: QWidget) -> None:
        """Open beside the mascot: above it when there is room, otherwise below, always inside the screen."""
        if self.size_mode == "maximized":
            self.show()
            self.raise_()
            self.activateWindow()
            self.refresh_state()
            return
        screen = (QApplication.screenAt(anchor.geometry().center()) or QApplication.primaryScreen()).availableGeometry()
        a = anchor.geometry()
        x = min(max(a.right() - self.width(), screen.left() + 8), screen.right() - self.width() - 8)
        y = a.top() - self.height() - 8
        if y < screen.top() + 8:
            y = min(a.bottom() + 8, screen.bottom() - self.height() - 8)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.refresh_state()
        QTimer.singleShot(150, lambda: self.clear_of(anchor))

    def clear_of(self, anchor: QWidget, margin: int = 8) -> None:
        g, a = self.geometry(), anchor.geometry()
        if g.intersects(a.adjusted(-margin, -margin, margin, margin)) and g.top() < a.top():
            self.move(self.x(), self.y() - (g.bottom() - (a.top() - margin)))

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.update_infobar()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.update_infobar()
        self.grip.move(self.width() - self.grip.width() - 1, self.height() - self.grip.height() - 1)
        self.grip.raise_()

    def keyPressEvent(self, e) -> None:
        asking = self.controller.current_req is not None
        if e.key() == Qt.Key_Escape:
            if asking:
                self.controller.answer(False)      # Esc = Deny
            elif self.in_history:
                self.hide_history()
            else:
                self.hide()
            return
        if asking and e.key() in (Qt.Key_Return, Qt.Key_Enter) and not self.allow_btn.hasFocus():
            self.controller.answer(False)          # Enter = Deny (the safe default)
            return
        super().keyPressEvent(e)
