"""
Talk to NINA: a small chat window next to the mascot, backed by one persistent managed agent (role `general` by default).

The conversation continues between runs: the Claude session id is kept in `chat.json` in the state directory and
resumed on the next message. Everything the agent wants to do beyond reading still goes through the Allow / Deny
dialog, exactly like the other managed agents.
"""
import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from nina.agents import managed, managed_view
from nina.agents import roles as roles_mod
from nina.i18n import _

BUSY = ("starting", "working")
LOG_STYLE = managed_view.LOG_STYLE


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
        self.asking = False
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

    def interrupt(self) -> None:
        if self.agent_id is not None and self.status == "working":
            self.message.emit("info", _("Interrupting..."))
            self.runner.interrupt(self.agent_id)

    def reset(self) -> None:
        """Forget the conversation: the next message starts a new Claude session."""
        if self.runner is not None and self.agent_id is not None:
            self.runner.stop(self.agent_id)
        self.agent_id = None
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
            self.set_status("error")
        elif kind == "permission":
            self.queue.append(payload)
            self.ask_next()

    def ask_next(self, parent=None) -> None:
        """One dialog at a time; each permission request only proceeds with your "Allow"."""
        if self.asking or not self.queue:
            return
        self.asking = True
        req = self.queue.pop(0)
        self.attention.emit(_("I need your permission!"))
        box = QMessageBox(QMessageBox.Question, _("Agent permission"),
                          _("{role} wants to use {tool}.", role=_((self.role or {}).get("title", "NINA")), tool=req["tool"]),
                          parent=parent)
        box.setInformativeText(req["detail"][:1800])
        allow = box.addButton(_("Allow"), QMessageBox.AcceptRole)
        deny = box.addButton(_("Deny"), QMessageBox.RejectRole)
        box.setDefaultButton(deny)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.exec()
        ok = box.clickedButton() is allow
        self.runner.resolve(req["req"], ok, "" if ok else _("Denied by the user."))
        self.message.emit("info", (_("Allowed") if ok else _("Denied")) + f": {req['tool']}")
        self.asking = False
        self.ask_next(parent)


class ChatBox(QWidget):
    """The window: transcript, input line and two small buttons."""

    def __init__(self, controller: ChatController):
        super().__init__()
        self.controller = controller
        self.setWindowTitle(_("Talk to NINA"))
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(400, 320)
        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        self.input = QLineEdit()
        self.input.setPlaceholderText(_("Message NINA..."))
        self.input.returnPressed.connect(self.submit)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #8a909c;")
        self.stop_btn = QPushButton(_("Interrupt"))
        self.stop_btn.clicked.connect(controller.interrupt)
        self.new_btn = QPushButton(_("New chat"))
        self.new_btn.clicked.connect(controller.reset)
        row = QHBoxLayout()
        row.addWidget(self.status, 1)
        row.addWidget(self.stop_btn)
        row.addWidget(self.new_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.input)
        layout.addLayout(row)
        controller.message.connect(self.on_message)
        controller.state_changed.connect(self.on_state)
        self.on_state(controller.status)

    def submit(self) -> None:
        text = self.input.text()
        if self.controller.ask(text):
            self.input.clear()

    def on_message(self, kind: str, text: str) -> None:
        if kind == "reset":
            self.log.clear()
            return
        who = {"user": _("you"), "assistant": "NINA"}.get(kind, "")
        prefix = f"<b>{who}:</b> " if who else ""
        body = managed_view.format_body(text)
        self.log.append(f"<div style='color:{LOG_STYLE.get(kind, '#e6e8ec')}; margin-bottom:6px'>{prefix}{body}</div>")

    def on_state(self, state: str) -> None:
        busy = state in BUSY
        self.input.setEnabled(not busy)
        self.stop_btn.setEnabled(state == "working")
        self.status.setText({"starting": _("starting..."), "working": _("thinking..."), "error": _("error")}.get(state, ""))
        if not busy:
            self.input.setFocus()

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
        # The window manager places and decorates the window after it is shown (frame and shadow are invisible to Qt
        # at this point), so check again once it has settled and keep a margin clear of the mascot.
        QTimer.singleShot(150, lambda: self.clear_of(anchor))
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def clear_of(self, anchor: QWidget, margin: int = 32) -> None:
        g, a = self.geometry(), anchor.geometry()
        if g.intersects(a.adjusted(-margin, -margin, margin, margin)) and g.top() < a.top():
            self.move(self.x(), self.y() - (g.bottom() - (a.top() - margin)))

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(e)
