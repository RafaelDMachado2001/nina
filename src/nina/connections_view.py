"""The "Connections and agents" window: the AIs NINA can use and which one each role runs on.

Tab "Connections": Claude accounts (discovered, read-only) and the OpenAI-compatible connections you added (add from a
preset, edit, remove, test, set as default). Tab "Agents": a connection (and optionally a model) per role.

Nothing is written until OK: then the config is saved (without keys) and keys go to the system keyring.
"""
import copy
import re
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from nina import connections, orchestration, secrets
from nina.agents import openai_client
from nina.i18n import _

TEST_PROMPT = "Reply with the word OK."


class _Bridge(QObject):
    done = Signal(object, object)


def thread_async(fn, done) -> None:
    """Run `fn` on a worker thread and call `done(result)` on the UI thread."""
    bridge = _Bridge()
    bridge.done.connect(lambda callback, result: callback(result))
    _keep.add(bridge)

    def work():
        try:
            result = fn()
        except Exception as e:      # shown to the user by `done`
            result = e
        bridge.done.emit(done, result)
        _keep.discard(bridge)

    threading.Thread(target=work, daemon=True).start()


_keep = set()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "connection"


class ConnectionForm(QDialog):
    """Add or edit one OpenAI-compatible connection."""

    def __init__(self, parent, conn: dict | None, keystore, client, run_async):
        super().__init__(parent)
        self.conn, self.keystore, self.client, self.run_async = conn, keystore, client, run_async
        self.setWindowTitle(_("Edit connection") if conn else _("Add a connection"))
        self.setMinimumWidth(520)
        self.preset = QComboBox()
        for key, p in connections.PRESETS.items():
            self.preset.addItem(p["name"], key)
        self.name = QLineEdit()
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://.../v1")
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        fetch = QPushButton(_("Fetch models"))
        fetch.clicked.connect(self.fetch_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(fetch)
        self.key_source = QComboBox()
        if keystore.keyring_usable():
            self.key_source.addItem(_("System keyring"), "keyring")
        self.key_source.addItem(_("Environment variable"), "env")
        self.key_source.addItem(_("No key"), "none")
        self.key_source.currentIndexChanged.connect(self.on_key_source)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.env_name = QLineEdit()
        self.env_name.setPlaceholderText("GEMINI_API_KEY")
        self.key_link = QLabel("")
        self.key_link.setOpenExternalLinks(True)
        self.context_window = QSpinBox()
        self.context_window.setRange(0, 100_000_000)
        self.context_window.setSpecialValueText(_("unknown"))
        self.price_in, self.price_out = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.price_in, self.price_out):
            spin.setRange(0, 10_000)
            spin.setDecimals(4)
            spin.setSuffix(" US$/M")
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 3600)
        self.timeout.setSuffix(" s")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #d08a4a;")

        form = QFormLayout()
        if conn is None:
            form.addRow(_("Type:"), self.preset)
        form.addRow(_("Name:"), self.name)
        form.addRow(_("Base URL:"), self.base_url)
        form.addRow(_("Model:"), model_row)
        form.addRow(_("API key:"), self.key_source)
        form.addRow("", self.secret)
        form.addRow("", self.env_name)
        form.addRow("", self.key_link)
        form.addRow(_("Context window:"), self.context_window)
        form.addRow(_("Price, input:"), self.price_in)
        form.addRow(_("Price, output:"), self.price_out)
        form.addRow(_("Timeout:"), self.timeout)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        if conn is None:
            self.preset.currentIndexChanged.connect(self.on_preset)
            self.timeout.setValue(300)
            self.on_preset()
        else:
            self.fill(conn)
        self.on_key_source()

    # ---- filling -----------------------------------------------------------------------------
    def fill(self, conn: dict) -> None:
        self.name.setText(conn.get("name", ""))
        self.base_url.setText(conn.get("base_url", ""))
        self.model.setCurrentText(conn.get("model", ""))
        key = conn.get("key") or {}
        idx = self.key_source.findData(key.get("source", "none"))
        self.key_source.setCurrentIndex(max(0, idx))
        self.env_name.setText(key.get("name", "") if key.get("source") == "env" else "")
        self.secret.setPlaceholderText(_("(stored: leave empty to keep it)") if key.get("source") == "keyring" else "")
        self.context_window.setValue(int(conn.get("context_window") or 0))
        self.price_in.setValue(float(conn.get("price_in") or 0))
        self.price_out.setValue(float(conn.get("price_out") or 0))
        self.timeout.setValue(int(conn.get("timeout_s") or 300))

    def on_preset(self) -> None:
        p = connections.PRESETS[self.preset.currentData()]
        if not self.name.text() or self.name.text() in {x["name"] for x in connections.PRESETS.values()}:
            self.name.setText(p["name"] if self.preset.currentData() != "custom" else "")
        self.base_url.setText(p["base_url"])
        wanted = ("keyring" if self.key_source.findData("keyring") >= 0 else "env") if p["needs_key"] else "none"
        self.key_source.setCurrentIndex(max(0, self.key_source.findData(wanted)))
        self.key_link.setText(f'<a href="{p["key_url"]}">{p["key_url"]}</a>' if p["key_url"] else "")

    def on_key_source(self) -> None:
        source = self.key_source.currentData()
        self.secret.setVisible(source == "keyring")
        self.env_name.setVisible(source == "env")

    # ---- reading -------------------------------------------------------------------------------
    def problem(self) -> str:
        """What is missing ("" when the form is complete)."""
        if not self.name.text().strip():
            return _("Give the connection a name.")
        if not re.match(r"^https?://\S+$", self.base_url.text().strip()):
            return _("Enter the base URL (http:// or https://).")
        if not self.model.currentText().strip():
            return _("Choose a model (Fetch models lists them).")
        if self.key_source.currentData() == "env" and not re.match(r"^[A-Za-z_]\w*$", self.env_name.text().strip()):
            return _("Enter the name of the environment variable that holds the key.")
        return ""

    def validate(self) -> None:
        problem = self.problem()
        if problem:
            self.status.setText(problem)
        else:
            self.accept()

    def key_spec(self) -> dict:
        source = self.key_source.currentData()
        if source == "env":
            return {"source": "env", "name": self.env_name.text().strip()}
        return {"source": source}

    def connection(self, conn_id: str) -> dict:
        old = self.conn or {}
        return {**old, "id": conn_id, "kind": "openai", "name": self.name.text().strip(),
                "base_url": self.base_url.text().strip().rstrip("/"), "model": self.model.currentText().strip(),
                "key": self.key_spec(), "context_window": self.context_window.value(),
                "price_in": self.price_in.value(), "price_out": self.price_out.value(),
                "timeout_s": self.timeout.value(), "headers": old.get("headers") or {}}

    def current_key(self) -> str:
        if self.secret.text():
            return self.secret.text()
        probe = {"id": (self.conn or {}).get("id", ""), "key": self.key_spec()}
        return self.keystore.get_key(probe)

    def fetch_models(self) -> None:
        base, key = self.base_url.text().strip(), self.current_key()
        self.status.setText(_("Asking the server for its models..."))

        def done(result) -> None:
            if isinstance(result, Exception):
                err = result if isinstance(result, openai_client.ApiError) else openai_client.ApiError(0, str(result))
                self.status.setText(self.client.friendly(err))
                return
            current = self.model.currentText()
            self.model.clear()
            self.model.addItems(result)
            self.model.setCurrentText(current or (result[0] if result else ""))
            self.status.setText(_("{n} models.", n=len(result)))

        def job():
            try:
                return self.client.list_models(base, key)
            except openai_client.ApiError as e:
                return e
        self.run_async(job, done)


class ConnectionsDialog(QDialog):
    def __init__(self, cfg: dict, accounts: list, save, parent=None, roles=None, client=openai_client,
                 keystore=secrets, run_async=None):
        super().__init__(parent)
        self.setWindowTitle(_("Connections and agents"))
        self.setMinimumSize(640, 460)
        self.cfg = {k: copy.deepcopy(cfg.get(k)) for k in ("connections", "default_connection", "role_connections")}
        self.cfg["orchestration"] = orchestration.settings(cfg)
        self.cfg["connections"] = list(self.cfg["connections"] or [])
        self.cfg["default_connection"] = self.cfg["default_connection"] or ""
        self.cfg["role_connections"] = dict(self.cfg["role_connections"] or {})
        self.accounts, self.save, self.roles = accounts, save, roles or {}
        self.client, self.keystore, self.run_async = client, keystore, run_async or thread_async
        self.pending_keys, self.removed = {}, set()
        self.rows = {}

        tabs = QTabWidget()
        tabs.addTab(self.build_connections_tab(), _("Connections"))
        tabs.addTab(self.build_agents_tab(), _("Agents"))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self.refresh()

    # ---- connections tab -------------------------------------------------------------------------
    def build_connections_tab(self) -> QWidget:
        page = QWidget()
        self.listing = QListWidget()
        self.listing.currentItemChanged.connect(lambda *_a: self.update_buttons())
        self.default_note = QLabel("")
        self.default_note.setWordWrap(True)
        self.default_note.setStyleSheet("color: #8a909c;")
        self.add_btn, self.edit_btn, self.remove_btn = QPushButton(_("Add...")), QPushButton(_("Edit...")), QPushButton(_("Remove"))
        self.default_btn, self.test_btn = QPushButton(_("Set as default")), QPushButton(_("Test"))
        self.follow_btn = QPushButton(_("Follow the Claude account menu"))
        self.add_btn.clicked.connect(lambda: self.open_form(None))
        self.edit_btn.clicked.connect(lambda: self.open_form(self.selected_id()))
        self.remove_btn.clicked.connect(lambda: self.remove(self.selected_id()))
        self.default_btn.clicked.connect(lambda: self.set_default(self.selected_id()))
        self.follow_btn.clicked.connect(lambda: self.set_default(None))
        self.test_btn.clicked.connect(self.test_selected)
        side = QVBoxLayout()
        for b in (self.add_btn, self.edit_btn, self.remove_btn, self.default_btn, self.follow_btn, self.test_btn):
            side.addWidget(b)
        side.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.listing, 1)
        row.addLayout(side)
        layout = QVBoxLayout(page)
        layout.addLayout(row)
        layout.addWidget(self.default_note)
        return page

    def all(self) -> list:
        return connections.all_connections(self.cfg, self.accounts)

    def connection_by_id(self, conn_id: str) -> dict | None:
        return next((c for c in self.all() if c["id"] == conn_id), None)

    def selected_id(self) -> str | None:
        item = self.listing.currentItem()
        return item.data(Qt.UserRole) if item else None

    def refresh(self) -> None:
        current = self.selected_id()
        self.listing.clear()
        default = self.cfg["default_connection"]
        for c in self.all():
            detail = _("Claude Code account") if c["kind"] == "claude" else f"{c['model']} · {c['base_url']}"
            mark = "  ★ " + _("default") if c["id"] == default else ""
            item = QListWidgetItem(f"{c['name']}{mark}\n    {detail}")
            item.setData(Qt.UserRole, c["id"])
            self.listing.addItem(item)
            if c["id"] == current:
                self.listing.setCurrentItem(item)
        self.default_note.setText(_("Default: {name}.", name=self.connection_by_id(default)["name"])
                                  if self.connection_by_id(default) else
                                  _("Default: the Claude account chosen in NINA's menu."))
        self.update_buttons()
        self.refresh_agent_choices()

    def update_buttons(self) -> None:
        conn = self.connection_by_id(self.selected_id() or "")
        custom = conn is not None and conn["kind"] == "openai"
        self.edit_btn.setEnabled(custom)
        self.remove_btn.setEnabled(custom)
        self.test_btn.setEnabled(custom)
        self.default_btn.setEnabled(conn is not None and conn["id"] != self.cfg["default_connection"])

    def make_form(self, conn: dict | None) -> ConnectionForm:
        return ConnectionForm(self, conn, self.keystore, self.client, self.run_async)

    def open_form(self, conn_id: str | None) -> None:
        conn = self.connection_by_id(conn_id) if conn_id else None
        form = self.make_form(conn)
        if form.exec() == QDialog.Accepted:
            self.accept_form(form, conn_id)

    def unique_id(self, name: str) -> str:
        taken = {c["id"] for c in self.all()} | {"claude"}
        base = slug(name)
        candidate, n = base, 1
        while candidate in taken or candidate.startswith("claude:"):
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def accept_form(self, form: ConnectionForm, existing_id: str | None) -> None:
        conn_id = existing_id or self.unique_id(form.name.text())
        conn = form.connection(conn_id)
        items = self.cfg["connections"]
        idx = next((i for i, c in enumerate(items) if isinstance(c, dict) and c.get("id") == conn_id), None)
        if idx is None:
            items.append(conn)
        else:
            items[idx] = conn
        if conn["key"]["source"] == "keyring" and form.secret.text():
            self.pending_keys[conn_id] = form.secret.text()
        self.removed.discard(conn_id)
        self.refresh()

    def remove(self, conn_id: str | None) -> None:
        if not conn_id:
            return
        self.cfg["connections"] = [c for c in self.cfg["connections"] if not (isinstance(c, dict) and c.get("id") == conn_id)]
        if self.cfg["default_connection"] == conn_id:
            self.cfg["default_connection"] = ""
        bindings = self.cfg["role_connections"]
        for role_id, value in list(bindings.items()):
            target = value.get("connection") if isinstance(value, dict) else value
            if target == conn_id:
                del bindings[role_id]
        self.pending_keys.pop(conn_id, None)
        self.removed.add(conn_id)
        self.refresh()
        self.load_bindings()

    def set_default(self, conn_id: str | None) -> None:
        self.cfg["default_connection"] = conn_id or ""
        self.refresh()

    def test_text(self, conn: dict) -> str:
        """Send a one-word request; the answer or a readable error. Blocking: run it off the UI thread."""
        key = self.pending_keys.get(conn["id"]) or self.keystore.get_key(conn)
        try:
            data = self.client.chat(conn["base_url"], key, conn["model"], [{"role": "user", "content": TEST_PROMPT}], [],
                                    min(float(conn.get("timeout_s") or 120), 120), conn.get("headers") or {})
            text = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
            return _("It works. {model} answered: {text}", model=conn["model"], text=text[:200] or "…")
        except openai_client.ApiError as e:
            return self.client.friendly(e)

    def test_selected(self) -> None:
        conn = self.connection_by_id(self.selected_id() or "")
        if conn is None or conn["kind"] != "openai":
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText(_("Testing..."))

        def done(text) -> None:
            self.test_btn.setText(_("Test"))
            self.update_buttons()
            QMessageBox.information(self, _("Test"), str(text))
        self.run_async(lambda: self.test_text(conn), done)

    # ---- agents tab --------------------------------------------------------------------------------
    def build_agents_tab(self) -> QWidget:
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.addWidget(QLabel("<b>" + _("Role") + "</b>"), 0, 0)
        grid.addWidget(QLabel("<b>" + _("AI") + "</b>"), 0, 1)
        grid.addWidget(QLabel("<b>" + _("Model (optional)") + "</b>"), 0, 2)
        ordered = sorted(self.roles.values(), key=lambda r: (r["id"] != "general", _(r["title"])))
        for i, role in enumerate(ordered, 1):
            combo, model = QComboBox(), QLineEdit()
            model.setPlaceholderText(_("the connection's model"))
            grid.addWidget(QLabel(_(role["title"])), i, 0)
            grid.addWidget(combo, i, 1)
            grid.addWidget(model, i, 2)
            self.rows[role["id"]] = {"connection": combo, "model": model}
            combo.currentIndexChanged.connect(lambda _i, rid=role["id"]: self.on_binding_changed(rid))
            model.editingFinished.connect(lambda rid=role["id"]: self.on_binding_changed(rid))
        grid.setRowStretch(len(ordered) + 1, 1)
        note = QLabel(_("Each role runs on its AI unless a chat or an agent picks another one. "
                        "Agents on other AIs use NINA's own tools, with the same permissions as Claude agents."))
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a909c;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.build_orchestration_box())
        layout.addWidget(scroll, 1)
        layout.addWidget(note)
        return page

    def build_orchestration_box(self) -> QGroupBox:
        st = self.cfg["orchestration"]
        box = QGroupBox(_("Orchestration"))
        self.orch_mode = QComboBox()
        for key, label in (("manual", _("Manual: you pick the agent")), ("coordinator", _("Coordinator: NINA delegates")),
                           ("collaborative", _("Collaborative: agents delegate too"))):
            self.orch_mode.addItem(label, key)
        self.orch_mode.setCurrentIndex(max(0, self.orch_mode.findData(st["mode"])))
        self.orch_depth, self.orch_budget = QSpinBox(), QSpinBox()
        self.orch_depth.setRange(1, 5)
        self.orch_depth.setValue(st["max_depth"])
        self.orch_budget.setRange(1, 50)
        self.orch_budget.setValue(st["max_delegations"])
        self.orch_ask = QCheckBox(_("Ask before each delegation"))
        self.orch_ask.setChecked(st["ask"])
        form = QFormLayout(box)
        form.addRow(_("Mode:"), self.orch_mode)
        form.addRow(_("Levels of delegation:"), self.orch_depth)
        form.addRow(_("Delegations per message:"), self.orch_budget)
        form.addRow("", self.orch_ask)
        return box

    def refresh_agent_choices(self) -> None:
        entries = [(None, _("Default"))] + [(c["id"], c["name"]) for c in self.all()]
        for row in self.rows.values():
            combo = row["connection"]
            combo.blockSignals(True)
            current = combo.currentData()
            combo.clear()
            for conn_id, label in entries:
                combo.addItem(label, conn_id)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.blockSignals(False)
        self.load_bindings()

    def load_bindings(self) -> None:
        for role_id, row in self.rows.items():
            value = self.cfg["role_connections"].get(role_id)
            conn_id, model = (value.get("connection"), value.get("model", "")) if isinstance(value, dict) else (value, "")
            row["connection"].blockSignals(True)
            row["connection"].setCurrentIndex(max(0, row["connection"].findData(conn_id)))
            row["connection"].blockSignals(False)
            row["model"].setText(model or "")

    def binding_row(self, role_id: str) -> dict:
        return self.rows[role_id]

    def on_binding_changed(self, role_id: str) -> None:
        row = self.rows[role_id]
        self.store_binding(role_id, row["connection"].currentData(), row["model"].text().strip())

    def store_binding(self, role_id: str, conn_id: str | None, model: str) -> None:
        bindings = self.cfg["role_connections"]
        if not conn_id:
            bindings.pop(role_id, None)
        else:
            bindings[role_id] = {"connection": conn_id, "model": model} if model else conn_id

    def set_binding(self, role_id: str, conn_id: str | None, model: str) -> None:
        self.store_binding(role_id, conn_id, model)
        if role_id in self.rows:
            self.load_bindings()

    # ---- closing -------------------------------------------------------------------------------------
    def accept(self) -> None:
        try:
            for conn_id in self.removed:
                self.keystore.delete_key(conn_id)
            for conn_id, value in self.pending_keys.items():
                self.keystore.set_key(conn_id, value)
        except Exception as e:
            QMessageBox.warning(self, _("Connections and agents"),
                                _("The key could not be stored: {error}", error=e))
            return
        self.cfg["orchestration"] = {**self.cfg["orchestration"], "mode": self.orch_mode.currentData(),
                                     "max_depth": self.orch_depth.value(), "max_delegations": self.orch_budget.value(),
                                     "ask": self.orch_ask.isChecked()}
        self.save(copy.deepcopy(self.cfg))
        super().accept()
