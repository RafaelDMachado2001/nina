"""
"Managed" tab of the panel: launch agents by role, talk to them, interrupt and close them.
Every permission request from an agent becomes an "Allow / Deny" dialog (nothing runs without your confirmation).
"""
import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from nina import connections, orchestration
from nina.agents import managed, managed_store
from nina.agents import roles as roles_mod
from nina.i18n import _


def claude_accounts(ctx: dict) -> list:
    return connections.claude_accounts(ctx.get("accounts"), ctx.get("account", ""), ctx.get("accounts_root"))


def resolve_for(ctx: dict, role: dict, explicit: str | None = None) -> tuple:
    """(connection, model, note) for an agent of `role`, explicitly on `explicit` or by the role's binding/default.
    A role's own binding model is kept when the explicit choice is that same connection."""
    cfg, accounts = ctx.get("cfg") or {}, claude_accounts(ctx)
    kw = dict(role_id=role["id"], role=role, active_account=ctx.get("account", "") or "", sdk=ctx.get("sdk", True))
    by_role = connections.resolve(cfg, accounts, explicit=None, **kw)
    if explicit is None or by_role[0]["id"] == explicit:
        return by_role
    return connections.resolve(cfg, accounts, explicit=explicit, **kw)

STATUS_SYMBOL = {"starting": "○", "working": "●", "idle": "✓", "closed": "✕", "error": "!"}
STATUS_COLOR = {"starting": "#8a909c", "working": "#7ed321", "idle": "#6fb3ff", "closed": "#6b7280", "error": "#f08c4a"}
LOG_STYLE = {"user": "#7ed321", "assistant": "#e6e8ec", "tool": "#8a909c", "denied": "#f08c4a",
             "error": "#ff6b6b", "result": "#6fb3ff", "info": "#8a909c"}


def format_body(text: str) -> str:
    """Escape `text` and apply the tiny Markdown subset used in the logs (**bold**, `code`, line breaks)."""
    body = html.escape(text)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)                      # **bold**
    body = re.sub(r"`([^`\n]+)`", r"<code style='color:#f2c9a0'>\1</code>", body)   # `code`
    return body.replace("\n", "<br>")


class NewAgentDialog(QDialog):
    def __init__(self, parent, roles: dict, ctx: dict):
        super().__init__(parent)
        self.setWindowTitle(_("New agent"))
        self.setMinimumWidth(560)
        self.roles = roles
        self.role = QComboBox()
        ordered = sorted(roles.values(), key=lambda r: (r["id"] != "general", _(r["title"])))
        for r in ordered:
            self.role.addItem(_(r["title"]), r["id"])
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color: #8a909c;")
        self.role.currentIndexChanged.connect(self.on_role)
        self.folder = QLineEdit(ctx.get("default_dir", ""))
        browse = QPushButton(_("Browse..."))
        browse.clicked.connect(self.browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.ctx = ctx
        self.connection = QComboBox()
        for conn in connections.all_connections(ctx.get("cfg") or {}, claude_accounts(ctx)):
            self.connection.addItem(conn["name"], conn["id"])
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(_("What do you want the agent to do?"))
        self.prompt.setMinimumHeight(110)
        form = QFormLayout()
        form.addRow(_("Role:"), self.role)
        form.addRow("", self.description)
        form.addRow(_("Folder:"), folder_row)
        form.addRow(_("AI:"), self.connection)
        form.addRow(_("Request:"), self.prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("Start"))
        buttons.button(QDialogButtonBox.Cancel).setText(_("Cancel"))
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.on_role()

    def on_role(self) -> None:
        role = self.roles[self.role.currentData()]
        idx = self.connection.findData(resolve_for(self.ctx, role)[0]["id"])
        self.connection.setCurrentIndex(max(0, idx))
        writes = ", ".join(role["writes"]) if role["writes"] else _("none (read-only)")
        self.description.setText(_(role["description"]) + "\n" + _("Writes allowed in: {writes}. Every write asks for confirmation.", writes=writes))

    def browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, _("Agent folder"), self.folder.text())
        if d:
            self.folder.setText(d)

    def validate(self) -> None:
        if not Path(self.folder.text()).expanduser().is_dir():
            QMessageBox.warning(self, _("New agent"), _("The folder does not exist."))
        elif not self.prompt.toPlainText().strip():
            QMessageBox.warning(self, _("New agent"), _("Write the request for the agent."))
        else:
            self.accept()

    def values(self) -> dict:
        return {"role": self.roles[self.role.currentData()],
                "cwd": str(Path(self.folder.text()).expanduser()),
                "connection": self.connection.currentData() or "claude",
                "prompt": self.prompt.toPlainText().strip()}


class ManagedTab(QWidget):
    attention = Signal(str)                  # short text for the mascot to say
    hook_event = Signal(dict)                # hook-style events of agents NINA runs itself, for the agents panel
    _event = Signal(str, str, object)        # emitted from the agents' thread, delivered on the UI thread

    def __init__(self, context, parent=None, store_path=None):
        """`context()` returns {accounts, account, accounts_root, default_dir, roles_dir, claude_bin}.
        `store_path` is where agents are remembered between runs (default: the state directory)."""
        super().__init__(parent)
        self.context = context
        self.agents = {}
        self.current = None
        self.queue = []
        self.asking = False
        self.store_path = store_path
        ctx = context()
        self.runner = managed.Runner(self._event.emit, managed.find_claude_bin(ctx.get("claude_bin", "")))
        self._event.connect(self.on_event)

        self.listing = QListWidget()
        self.listing.currentItemChanged.connect(self.on_select)
        new_btn = QPushButton(_("New agent..."))
        new_btn.clicked.connect(self.new_agent)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 8, 0)
        ll.addWidget(new_btn)
        ll.addWidget(self.listing, 1)

        self.header = QLabel(_("Create an agent to get started."))
        self.header.setTextFormat(Qt.RichText)
        self.header.setWordWrap(True)
        self.log = QTextBrowser()
        self.input = QLineEdit()
        self.input.setPlaceholderText(_("Message the agent..."))
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton(_("Send"))
        self.send_btn.clicked.connect(self.send)
        self.stop_turn_btn = QPushButton(_("Interrupt"))
        self.stop_turn_btn.clicked.connect(self.interrupt)
        self.resume_btn = QPushButton(_("Resume"))
        self.resume_btn.clicked.connect(self.resume_agent)
        self.close_btn = QPushButton(_("Close"))
        self.close_btn.clicked.connect(self.close_agent)
        row = QHBoxLayout()
        for w in (self.input, self.send_btn, self.stop_turn_btn, self.resume_btn, self.close_btn):
            row.addWidget(w, 1 if w is self.input else 0)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.addWidget(self.header)
        rl.addWidget(self.log, 1)
        rl.addLayout(row)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(split)
        self.restore()
        self.refresh_controls()

    # ---- lifecycle ---------------------------------------------------------------------------
    def launch(self, role: dict, cwd: str, prompt, connection_id: str | None, resume: str | None = None) -> tuple:
        """Start (or resume) an agent of `role` on a connection. Returns (agent id, connection, note)."""
        ctx = self.context()
        cfg = ctx.get("cfg") or {}
        self.runner.deny = list((cfg.get("permissions") or {}).get("deny") or [])
        self.runner.max_steps = connections.max_steps(cfg)
        self.runner.connections = connections.custom_connections(cfg)
        conn, model, note = resolve_for(ctx, role, connection_id)
        self.runner.orchestration = orchestration.runtime(cfg, claude_accounts(ctx), roles_mod.load_roles(ctx.get("roles_dir")),
                                                          ctx.get("account", "") or "", ctx.get("sdk", True),
                                                          ctx.get("accounts_root"), conn)
        if resume and conn["id"] != connection_id:      # a session only resumes on the connection it ran on
            resume = None
            note = (note + " " if note else "") + _("A new conversation starts.")
        account = conn.get("account", "") if conn["kind"] == "claude" else ""
        config_dir = None
        if account and ctx.get("accounts_root") and (Path(ctx["accounts_root"]) / account).is_dir():
            config_dir = str(Path(ctx["accounts_root"]) / account)
        agent_id = self.runner.start(role, cwd, prompt, account, config_dir, resume=resume, connection=conn, model=model)
        return agent_id, conn, note

    def new_agent(self) -> None:
        ctx = self.context()
        dialog = NewAgentDialog(self, roles_mod.load_roles(ctx.get("roles_dir")), ctx)
        if dialog.exec() != QDialog.Accepted:
            return
        v = dialog.values()
        agent_id, conn, note = self.launch(v["role"], v["cwd"], v["prompt"], v["connection"])
        item = QListWidgetItem()
        item.setData(Qt.UserRole, agent_id)
        self.agents[agent_id] = {"role": v["role"], "cwd": v["cwd"], "account": conn.get("account", ""),
                                 "connection": conn["id"], "status": "starting",
                                 "log": [], "session": None, "cost": 0.0, "item": item}
        self.listing.addItem(item)
        if note:
            self.add_log(agent_id, "info", note)
        self.add_log(agent_id, "user", v["prompt"])
        self.update_item(agent_id)
        self.listing.setCurrentItem(item)

    def send(self) -> None:
        text = self.input.text().strip()
        if not text or self.current not in self.agents or self.agents[self.current]["status"] != "idle":
            return
        self.input.clear()
        self.add_log(self.current, "user", text)
        ctx = self.context()          # the orchestration settings as they are now (the mode may have changed)
        a = self.agents[self.current]
        conn = connections.get(ctx.get("cfg") or {}, claude_accounts(ctx), a.get("connection") or "claude") or {"id": "claude", "kind": "claude", "name": "Claude"}
        self.runner.orchestration = orchestration.runtime(ctx.get("cfg") or {}, claude_accounts(ctx),
                                                          roles_mod.load_roles(ctx.get("roles_dir")), ctx.get("account", "") or "",
                                                          ctx.get("sdk", True), ctx.get("accounts_root"), conn)
        self.agents[self.current]["status"] = "working"
        self.runner.send(self.current, text)
        self.update_item(self.current)
        self.refresh_controls()

    def interrupt(self) -> None:
        if self.current in self.agents:
            self.add_log(self.current, "info", _("Interrupting..."))
            self.runner.interrupt(self.current)

    def close_agent(self) -> None:
        a = self.agents.get(self.current)
        if a is None:
            return
        if a["status"] in ("closed", "error"):     # already stopped: this button forgets the agent
            self.forget(self.current)
        else:
            self.runner.stop(self.current)

    # ---- remembering agents between runs ---------------------------------------------------------
    def persist(self) -> None:
        managed_store.save([{"role": a["role"]["id"], "cwd": a["cwd"], "account": a["account"],
                             "connection": a.get("connection") or "claude",
                             "session": a["session"], "cost": a["cost"], "log": a["log"]}
                            for a in self.agents.values()], self.store_path)

    def restore(self) -> None:
        """Bring back the agents saved by a previous run as closed entries that can be resumed."""
        known = roles_mod.load_roles(self.context().get("roles_dir"))
        for i, saved in enumerate(managed_store.load(self.store_path)):
            role = known.get(saved["role"])
            if role is None:
                continue
            item = QListWidgetItem()
            agent_id = f"saved-{i}"
            item.setData(Qt.UserRole, agent_id)
            self.agents[agent_id] = {"role": role, "cwd": saved["cwd"], "account": saved["account"],
                                     "connection": saved["connection"], "status": "closed",
                                     "log": list(saved["log"]), "session": saved["session"], "cost": saved["cost"],
                                     "item": item}
            self.listing.addItem(item)
            self.update_item(agent_id)

    def forget(self, agent_id: str) -> None:
        a = self.agents.pop(agent_id, None)
        if a is None:
            return
        self.listing.takeItem(self.listing.row(a["item"]))
        self.persist()
        self.refresh_controls()

    def resume_agent(self) -> None:
        a = self.agents.get(self.current)
        if a is None or a["status"] not in ("closed", "error") or not a["session"]:
            return
        old_id, item = self.current, a["item"]
        new_id, _conn, note = self.launch(a["role"], a["cwd"], None, a.get("connection") or "claude", resume=a["session"])
        item.setData(Qt.UserRole, new_id)
        self.agents[new_id] = {**self.agents.pop(old_id), "status": "starting"}
        self.current = new_id
        self.add_log(new_id, "info", note or _("Conversation resumed."))
        self.update_item(new_id)
        self.refresh_controls()

    def shutdown(self) -> None:
        self.runner.shutdown()

    # ---- agent events --------------------------------------------------------------------------
    def on_event(self, agent_id: str, kind: str, payload) -> None:
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        if kind == "status":
            agent["status"] = payload if payload in STATUS_SYMBOL else agent["status"]
            if payload == "closed":
                self.add_log(agent_id, "info", _("Agent closed."))
        elif kind == "session":
            agent["session"] = payload
        elif kind == "text":
            self.add_log(agent_id, "assistant", payload)
        elif kind == "tool":
            self.add_log(agent_id, "tool", f"{payload['name']}: {payload['summary']}")
        elif kind == "denied":
            self.add_log(agent_id, "denied", _("Blocked ({tool}): {why}", tool=payload["tool"], why=payload["why"]))
        elif kind == "result":
            agent["cost"] += payload.get("cost") or 0.0
            note = _("turn finished · {steps} step(s) · US$ {cost}", steps=payload.get("turns") or "?", cost=f"{agent['cost']:.4f}")
            if payload.get("error") or payload.get("errors"):
                self.add_log(agent_id, "error", note + " · " + "; ".join(payload.get("errors") or [_("error")]))
            else:
                self.add_log(agent_id, "result", note)
        elif kind == "error":
            agent["status"] = "error"
            self.add_log(agent_id, "error", str(payload))
        elif kind == "permission":
            self.queue.append((agent_id, payload))
            self.ask_next()
        elif kind == "permission_withdrawn":
            self.queue = [(aid, r) for aid, r in self.queue if r.get("req") != payload.get("req")]
        elif kind == "turn_error":            # an answer failed (quota, key, network...): the agent is still ready
            agent["status"] = "idle"
            self.add_log(agent_id, "error", str(payload))
        elif kind == "hook":
            self.hook_event.emit(dict(payload))
        if kind in ("session", "result") or (kind == "status" and payload == "closed"):
            self.persist()
        self.update_item(agent_id)
        self.refresh_controls()

    def ask_next(self) -> None:
        """One dialog at a time; each permission request only proceeds with your "Allow"."""
        if self.asking or not self.queue:
            return
        self.asking = True
        agent_id, req = self.queue.pop(0)
        agent = self.agents[agent_id]
        self.attention.emit(_("I need your permission!"))
        box = QMessageBox(QMessageBox.Question, _("Agent permission"),
                          _("{role} wants to use {tool}.", role=_(agent["role"]["title"]), tool=req["tool"]), parent=self)
        box.setInformativeText(req["detail"][:1800])
        allow = box.addButton(_("Allow"), QMessageBox.AcceptRole)
        deny = box.addButton(_("Deny"), QMessageBox.RejectRole)
        box.setDefaultButton(deny)
        box.exec()
        ok = box.clickedButton() is allow
        self.runner.resolve(req["req"], ok, "" if ok else _("Denied by the user."))
        self.add_log(agent_id, "info", (_("Allowed") if ok else _("Denied")) + f": {req['tool']}")
        self.asking = False
        self.ask_next()

    # ---- presentation ---------------------------------------------------------------------------
    def add_log(self, agent_id: str, kind: str, text: str) -> None:
        self.agents[agent_id]["log"].append((kind, text))
        if agent_id == self.current:
            self.append_line(kind, text)

    def append_line(self, kind: str, text: str) -> None:
        who = {"user": _("you"), "assistant": _("agent")}.get(kind, "")
        prefix = f"<b>{who}:</b> " if who else ""
        body = format_body(text)
        self.log.append(f"<div style='color:{LOG_STYLE.get(kind, '#e6e8ec')}; margin-bottom:6px'>{prefix}{body}</div>")

    def update_item(self, agent_id: str) -> None:
        a = self.agents[agent_id]
        a["item"].setText(f"{STATUS_SYMBOL.get(a['status'], '?')}  {_(a['role']['title'])} · {Path(a['cwd']).name}")

    def on_select(self, item, _prev) -> None:
        self.current = item.data(Qt.UserRole) if item else None
        self.log.clear()
        if self.current in self.agents:
            for kind, text in self.agents[self.current]["log"]:
                self.append_line(kind, text)
        self.refresh_controls()

    def refresh_controls(self) -> None:
        a = self.agents.get(self.current)
        alive = a is not None and a["status"] in ("idle", "working")
        self.input.setEnabled(a is not None and a["status"] == "idle")
        self.send_btn.setEnabled(a is not None and a["status"] == "idle")
        self.stop_turn_btn.setEnabled(a is not None and a["status"] == "working")
        stopped = a is not None and a["status"] in ("closed", "error")
        self.resume_btn.setEnabled(stopped and bool(a["session"]))
        self.close_btn.setText(_("Remove") if stopped else _("Close"))
        self.close_btn.setEnabled(alive or stopped)
        if a is None:
            self.header.setText(_("Create an agent to get started."))
            return
        color = STATUS_COLOR.get(a["status"], "#8a909c")
        label = {"starting": _("starting"), "working": _("working"), "idle": _("waiting for your message"),
                 "closed": _("closed"), "error": _("error")}.get(a["status"], a["status"])
        session = " · " + _("session {id}", id=a["session"][:8]) if a["session"] else ""
        self.header.setText(f"<b>{_(a['role']['title'])}</b> · <span style='color:{color}'>● {label}</span>{session}<br>"
                            f"<span style='color:#8a909c'>{a['cwd']} · " + self.connection_label(a) + "</span>")

    def connection_label(self, a: dict) -> str:
        ctx = self.context()
        conn = connections.get(ctx.get("cfg") or {}, claude_accounts(ctx), a.get("connection") or "claude")
        return conn["name"] if conn else str(a.get("connection") or "")
