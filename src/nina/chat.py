"""
Talk to NINA: a small chat window next to the mascot, backed by one persistent managed agent (role `general` by default).

The conversation continues between runs: the Claude session id is kept in `chat.json` in the state directory and
resumed on the next message. Everything the agent wants to do beyond reading still goes through the Allow / Deny
dialog, exactly like the other managed agents.
"""
import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from nina import chat_theme, config
from nina.agents import managed
from nina.agents import roles as roles_mod
from nina.i18n import _

BUSY = ("starting", "working")


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nina" / "chat.json"


def load_saved(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or state_path()).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("session"), str) and data["session"] else {}


def save_saved(data: dict, path: Path | None = None) -> None:
    target = path or state_path()
    try:
        if not data:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError:
        pass


class ChatController(QObject):
    """One persistent conversation. The UI thread talks to it; the agent runs on the Runner's own thread."""
    message = Signal(str, str)        # kind (user|assistant|tool|denied|error|info), text
    state_changed = Signal(str)       # none | starting | working | idle | closed | error
    attention = Signal(str)           # short text for the mascot to say (permission requests)
    permission_asked = Signal(dict)   # {"req", "tool", "detail"}: the window shows Allow / Deny
    permission_cleared = Signal()
    finished = Signal()               # an answer is complete
    _event = Signal(str, str, object)   # from the Runner's thread, delivered on the UI thread

    def __init__(self, context, runner_factory=None, path: Path | None = None, parent=None):
        """`context()` returns the same dict as the managed tab, plus an optional "chat_role"."""
        super().__init__(parent)
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
        self._event.connect(self.on_event)

    # ---- API (UI thread) ---------------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.status in BUSY

    def role_for_chat(self) -> dict:
        known = roles_mod.load_roles(self.context().get("roles_dir"))
        return known.get(self.context().get("chat_role") or "general") or known["general"]

    def ask(self, text: str) -> bool:
        """Send a message. Returns False when it was not sent (empty, or the agent is still answering)."""
        text = (text or "").strip()
        if not text or self.busy:
            return False
        self.last_user = text
        self.message.emit("user", text)
        if self.agent_id is not None and self.status == "idle":
            self.set_status("working")
            self.runner.send(self.agent_id, text)
        else:
            self.start(text)
        return True

    def start(self, prompt: str) -> None:
        ctx = self.context()
        if self.runner is None:
            self.runner = self.runner_factory(self._event.emit, managed.find_claude_bin(ctx.get("claude_bin", "")))
        account = ctx.get("account", "") or ""
        config_dir = None
        if account and (Path(ctx["accounts_root"]) / account).is_dir():
            config_dir = str(Path(ctx["accounts_root"]) / account)
        saved = load_saved(self.path)
        self.resuming = bool(saved)
        self.role = self.role_for_chat()
        self.set_status("starting")
        self.agent_id = self.runner.start(self.role, ctx["default_dir"], prompt, account, config_dir,
                                          resume=saved.get("session"))

    def retry(self) -> bool:
        """Send the last message again (after an error)."""
        return self.ask(self.last_user)

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
            ctx = self.context()
            save_saved({"session": payload, "account": ctx.get("account", ""), "cwd": ctx["default_dir"]}, self.path)
        elif kind == "text":
            self.message.emit("assistant", payload)
        elif kind == "tool":
            self.message.emit("tool", f"{payload['name']}: {payload['summary']}")
        elif kind == "denied":
            self.message.emit("denied", _("Blocked ({tool}): {why}", tool=payload["tool"], why=payload["why"]))
        elif kind == "result":
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

    def answer(self, allow: bool) -> None:
        req, self.current_req = self.current_req, None
        if req is None:
            return
        self.runner.resolve(req["req"], allow, "" if allow else _("Denied by the user."))
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
    pen.setCapStyle(Qt.RoundCap if kind in ("dots", "close") else Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    if kind == "dots":
        p.setBrush(QColor(color))
        for cy in (5, 12, 19):
            p.drawEllipse(QPointF(12, cy), 1.2, 1.2)
    elif kind == "close":
        p.drawLine(6, 6, 18, 18)
        p.drawLine(18, 6, 6, 18)
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
        if e.button() == Qt.LeftButton and handle is not None:
            handle.startSystemMove()
        super().mousePressEvent(e)


class ChatBox(QWidget):
    """The frameless chat window: header, messages (or the empty state), thinking indicator and composer
    (replaced by the permission sheet while the agent waits for you)."""
    MIN_W, MIN_H = 360, 420

    def __init__(self, controller: ChatController, theme: str = "dark", on_theme=None):
        super().__init__()
        self.controller = controller
        self.theme = theme if theme in chat_theme.THEMES else "dark"
        self.on_theme = on_theme
        self.entries = []                     # (kind, text), kept to re-render when the theme changes
        self.setObjectName("ChatWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle(_("Talk to NINA"))
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.resize(400, 520)
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
            b.clicked.connect(lambda _=False, text=text: controller.ask(text))
            self.suggestions.append(b)
            sl.addWidget(b)
        el.addLayout(sl)
        self.body = QStackedWidget()
        self.body.addWidget(self.empty)
        self.body.addWidget(self.log)

        self.thinking = ThinkingDots()

        # composer and permission sheet
        self.composer = QWidget()
        self.composer.setObjectName("Composer")
        self.composer.setAttribute(Qt.WA_StyledBackground, True)
        self.input = QLineEdit()
        self.input.setPlaceholderText(_("Message NINA..."))
        self.input.setFixedHeight(36)
        self.input.returnPressed.connect(self.submit)
        self.stop_btn = QPushButton(_("Interrupt"))
        self.stop_btn.setObjectName("Interrupt")
        self.stop_btn.setFixedHeight(36)
        self.stop_btn.clicked.connect(controller.interrupt)
        self.send_btn = QPushButton()
        self.send_btn.setObjectName("Send")
        self.send_btn.setToolTip(_("Send"))
        self.send_btn.clicked.connect(self.submit)
        cl = QHBoxLayout(self.composer)
        cl.setContentsMargins(10, 10, 10, 10)
        cl.setSpacing(8)
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
        self.deny_btn.clicked.connect(lambda: controller.answer(False))
        self.allow_btn = QPushButton(_("Allow"))
        self.allow_btn.setObjectName("Allow")
        self.allow_btn.setFixedHeight(36)
        self.allow_btn.setToolTip("Alt+P")
        self.allow_btn.clicked.connect(lambda: controller.answer(True))
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(self.perm_icon)
        title_row.addWidget(self.perm_title, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.deny_btn)
        buttons.addWidget(self.allow_btn)
        buttons.addStretch(1)
        sheet_layout = QVBoxLayout(self.sheet)
        sheet_layout.setContentsMargins(12, 12, 12, 12)
        sheet_layout.setSpacing(8)
        sheet_layout.addLayout(title_row)
        sheet_layout.addWidget(self.perm_command)
        sheet_layout.addWidget(self.perm_where)
        sheet_layout.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.thinking)
        layout.addWidget(self.composer)
        layout.addWidget(self.sheet)
        self.sheet.hide()

        self.grip = QSizeGrip(self)                  # resize from the bottom-right corner
        self.grip.setFixedSize(14, 14)
        self.grip.setStyleSheet("background: transparent;")
        QShortcut(QKeySequence("Ctrl+N"), self, activated=controller.reset)
        QShortcut(QKeySequence("Alt+P"), self, activated=self.allow_shortcut)

        controller.message.connect(self.on_message)
        controller.state_changed.connect(lambda _s: self.refresh_state())
        controller.permission_asked.connect(self.on_permission)
        controller.permission_cleared.connect(self.refresh_state)
        self.apply_theme(self.theme)

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
        new.triggered.connect(self.controller.reset)
        menu.addSeparator()
        other = "light" if self.theme == "dark" else "dark"
        menu.addAction(_("Light theme") if other == "light" else _("Dark theme")).triggered.connect(
            lambda: self.set_theme(other))
        menu.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))

    # ---- messages ----------------------------------------------------------------------------------
    def on_message(self, kind: str, text: str) -> None:
        if kind == "reset":
            self.entries.clear()
            self.log.clear()
            self.body.setCurrentWidget(self.empty)
            return
        self.entries.append((kind, text))
        self.body.setCurrentWidget(self.log)
        self.append(kind, text)

    def html_for(self, kind: str, text: str) -> str:
        th = self.theme
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
        self.status.setText({"ready": _("ready"), "thinking": _("thinking"), "perm": _("needs permission"),
                             "error": _("error")}[key])
        self.dot.set_color({"ready": t["accent"], "thinking": t["indigo"], "perm": t["warn"], "error": t["err_text"]}[key])
        busy = self.controller.busy
        asking = self.controller.current_req is not None
        self.input.setEnabled(not busy)
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setVisible(self.controller.status == "working")
        if busy and not asking:
            if not self.thinking.isVisible():
                self.thinking.start()
        else:
            self.thinking.stop()
        self.composer.setVisible(not asking)
        self.sheet.setVisible(asking)
        if asking:
            self.deny_btn.setFocus()
        elif not busy:
            self.input.setFocus()
        else:
            self.log.setFocus()            # keeps the focus ring off the header buttons while it works

    def on_permission(self, req: dict) -> None:
        self.perm_title.setText(_("NINA wants to use {tool}", tool=req["tool"]))
        self.perm_command.setText(str(req.get("detail", ""))[:900])
        where = (self.controller.context() or {}).get("default_dir", "")
        self.perm_where.setText(_("in {where}", where=config.short_path(where)) if where else "")
        self.refresh_state()
        if not self.isVisible():
            self.show()
            self.raise_()

    def allow_shortcut(self) -> None:
        if self.controller.current_req is not None:
            self.controller.answer(True)

    def submit(self) -> None:
        if self.controller.ask(self.input.text()):
            self.input.clear()

    # ---- placement and keys --------------------------------------------------------------------------
    def show_near(self, anchor: QWidget) -> None:
        """Open beside the mascot: above it when there is room, otherwise below, always inside the screen."""
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

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.grip.move(self.width() - self.grip.width() - 1, self.height() - self.grip.height() - 1)
        self.grip.raise_()

    def keyPressEvent(self, e) -> None:
        asking = self.controller.current_req is not None
        if e.key() == Qt.Key_Escape:
            if asking:
                self.controller.answer(False)      # Esc = Deny
            else:
                self.hide()
            return
        if asking and e.key() in (Qt.Key_Return, Qt.Key_Enter) and not self.allow_btn.hasFocus():
            self.controller.answer(False)          # Enter = Deny (the safe default)
            return
        super().keyPressEvent(e)
