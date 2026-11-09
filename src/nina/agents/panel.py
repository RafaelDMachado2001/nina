"""
"Agents" panel: lists Claude Code sessions and subagents, the state of each one,
the timeline of actions and the conversation (from the transcript, when available).
Read-only: it does not control the agents, it only shows what hooks and transcripts report.
"""
import re
import shlex
import sys
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from nina import launcher
from nina.agents import transcript
from nina.agents.graph import build_graph, pulses_for
from nina.agents.graph_view import STATUS_COLOR, GraphView
from nina.agents.store import AgentStore, clip
from nina.i18n import N_, _

STATUS = {   # state -> (symbol, color, label); labels are translated where they are displayed
    "working": ("●", "#7ed321", N_("working")),
    "waiting": ("●", "#ffd400", N_("waiting for you")),
    "done": ("✓", "#6fb3ff", N_("finished its work")),
    "idle": ("○", "#8a909c", N_("idle")),
    "ended": ("✕", "#6b7280", N_("session ended")),
    "stale": ("●", "#f08c4a", N_("no signal")),
}
HOOK_TO_TYPE = {"UserPromptSubmit": "prompt", "PreToolUse": "tool", "SubagentStart": "start",
                "Stop": "done", "SubagentStop": "done", "Notification": "waiting"}
HINT_READONLY = N_("Read-only: NINA shows what the hooks report.")
REPLAY_STEP = 4          # slider step per tick (0..1000), ~25 s for the whole replay
ROLE_LABEL = {"user": N_("you"), "assistant": N_("agent"), "tool": N_("tool"), "result": N_("result")}
STYLE = """
QWidget { background: #14161a; color: #e6e8ec; font-size: 13px; }
QTreeWidget, QListWidget, QPlainTextEdit { background: #1b1e24; border: 1px solid #2a2d34; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #2f4a14; color: #ffffff; }
QHeaderView::section { background: #2a2d34; color: #c4c9d2; border: 0; padding: 4px; }
QTabBar::tab { background: #2a2d34; padding: 6px 12px; }
QTabBar::tab:selected { background: #4e9a12; color: #ffffff; }
QPushButton { background: #2a2d34; border: 1px solid #454b57; padding: 6px 12px; border-radius: 4px; }
QPushButton:hover { background: #454b57; }
"""


def ago(ts: float, now: float) -> str:
    s = max(0, int(now - ts))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def folder(cwd: str | None) -> str:
    return re.split(r"[/\\]", (cwd or "").rstrip("/\\"))[-1] or "—"


class AgentsPanel(QWidget):
    def __init__(self, store: AgentStore, managed_tab=None, on_resume=None):
        """`on_resume(cwd, session_id, transcript)` reopens a session in a terminal; without it the button is hidden."""
        super().__init__()
        self.store = store
        self.managed_tab = managed_tab
        self.on_resume = on_resume
        self.selected = None
        self.setWindowTitle(_("NINA — Agents"))
        self.resize(1160, 600)
        self._titles = {}   # cache: subagent descriptions (read from the .meta.json)
        self.setStyleSheet(STYLE)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["", _("Agent"), _("Folder"), _("Activity"), _("Ago")])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for col, width in ((0, 28), (1, 210), (2, 110), (4, 56)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.tree.setColumnWidth(col, width)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.currentItemChanged.connect(self.on_select)
        self.tree.setMinimumWidth(600)

        self.header = QLabel(_("Select an agent"))
        self.header.setTextFormat(Qt.RichText)
        self.header.setWordWrap(True)
        self.timeline = QListWidget()
        self.conversation = QPlainTextEdit()
        self.conversation.setReadOnly(True)
        self.conversation.setFont(QFont("monospace", 10))
        self.tabs = QTabWidget()
        self.tabs.addTab(self.timeline, _("Timeline"))
        self.tabs.addTab(self.conversation, _("Conversation"))
        self.tabs.currentChanged.connect(lambda _i: self.show_details(self.selected))

        self.resume_btn = QPushButton(_("Copy resume command"))
        self.resume_btn.clicked.connect(self.copy_resume)
        self.terminal_btn = QPushButton(_("Open in terminal"))
        self.terminal_btn.clicked.connect(self.open_in_terminal)
        self.terminal_btn.setVisible(on_resume is not None)
        self.clear_btn = QPushButton(_("Clear ended"))
        self.clear_btn.clicked.connect(self.clear_finished)
        self.hint = QLabel(_(HINT_READONLY))
        self.hint.setStyleSheet("color: #8a909c;")
        buttons = QHBoxLayout()
        buttons.addWidget(self.resume_btn)
        buttons.addWidget(self.terminal_btn)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.hint)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.addWidget(self.header)
        rl.addWidget(self.tabs, 1)
        rl.addLayout(buttons)
        self.split = QSplitter()
        self.split.addWidget(self.tree)
        self.split.addWidget(right)
        self.split.setChildrenCollapsible(False)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        # ---- graph tab ----------------------------------------------------
        self.graph_view = GraphView()
        self.live = QCheckBox(_("Live"))
        self.live.setChecked(True)
        self.live.toggled.connect(self.on_live_toggled)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(1000)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self.on_slider)
        self.play_btn = QPushButton(_("▶ Play"))
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self.toggle_play)
        self.time_label = QLabel(_("live"))
        self.time_label.setMinimumWidth(130)
        controls = QHBoxLayout()
        for w in (self.live, self.play_btn, self.slider, self.time_label):
            controls.addWidget(w, 1 if w is self.slider else 0)
        graph_tab = QWidget()
        gl = QVBoxLayout(graph_tab)
        gl.setContentsMargins(0, 6, 0, 0)
        gl.addWidget(self.graph_view, 1)
        gl.addLayout(controls)
        self.replay = {"agents": [], "events": [], "t0": 0.0, "t1": 0.0, "cut": 0.0}
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self.play_tick)

        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self.split, _("Agents"))
        self.main_tabs.addTab(graph_tab, _("Graph"))
        if managed_tab is not None:
            self.main_tabs.addTab(managed_tab, _("Managed"))
        self.main_tabs.currentChanged.connect(lambda _i: self.refresh_graph())
        layout = QVBoxLayout(self)
        layout.addWidget(self.main_tabs)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.refresh()

    # ---- data ------------------------------------------------------------
    def _subagent_title(self, a: dict) -> str:
        """Subagent description (the .meta.json next to the transcript), cached."""
        key = a["id"]
        if key not in self._titles:
            title = ""
            if a.get("transcript"):
                _jsonl, meta = transcript.subagent_files(a["transcript"], key.split("/", 1)[1])
                title = str(transcript.read_meta(meta).get("description") or "")
            self._titles[key] = title
        return self._titles[key]

    def _session_title(self, a: dict) -> str:
        """Session title: the first request seen, or the automatic title from the transcript."""
        if a["title"]:
            return a["title"]
        if a["id"] not in self._titles and a.get("transcript"):
            self._titles[a["id"]] = transcript.find_title(a["transcript"])
        return self._titles.get(a["id"], "")

    def _item(self, a: dict, now: float, parent=None) -> QTreeWidgetItem:
        symbol, color, _label = STATUS.get(a["display"], STATUS["idle"])
        if a["kind"] == "subagent":
            title = a["title"] or self._subagent_title(a)
            name = f"{a['agent_type'] or _('agent')}: {title}" if title else (a["agent_type"] or _("subagent"))
        else:
            name = self._session_title(a) or _("session")
        item = QTreeWidgetItem(parent or self.tree, [symbol, name, folder(a["cwd"]), a["activity"] or "", ago(a["last_event_at"], now)])
        item.setForeground(0, QBrush(QColor(color)))
        item.setData(0, Qt.UserRole, a["id"])
        item.setToolTip(3, a["activity"] or "")
        return item

    def refresh(self) -> None:
        if not self.isVisible() and self.tree.topLevelItemCount():
            return
        now = time.time()
        agents = self.store.list_agents(now)
        keep = self.selected
        self.tree.blockSignals(True)
        self.tree.clear()
        restore = None
        for a in agents:
            top = self._item(a, now)
            if a["id"] == keep:
                restore = top
            for child in a["children"]:
                it = self._item(child, now, top)
                if child["id"] == keep:
                    restore = it
            top.setExpanded(True)
        self.tree.blockSignals(False)
        if restore is not None:
            self.tree.setCurrentItem(restore)
        self.show_details(keep)
        if self.main_tabs.currentIndex() == 1 and self.live.isChecked():
            self.refresh_graph()

    def on_select(self, current, _previous) -> None:
        self.selected = current.data(0, Qt.UserRole) if current else None
        self.show_details(self.selected)

    def show_details(self, agent_id) -> None:
        agent = self.store.get(agent_id) if agent_id else None
        if agent is None:
            self.header.setText(_("Select an agent"))
            self.timeline.clear()
            self.conversation.setPlainText("")
            return
        _s, color, label = STATUS.get(agent["display"], STATUS["idle"])
        started = datetime.fromtimestamp(agent["started_at"]).strftime("%d/%m %H:%M")
        kind = _("subagent ({type})", type=agent["agent_type"]) if agent["kind"] == "subagent" else _("session")
        title = agent["title"] or (self._subagent_title(agent) if agent["kind"] == "subagent" else self._session_title(agent))
        self.header.setText(
            f"<b>{(title or kind)[:90]}</b><br>"
            f"<span style='color:{color}'>● {_(label)}</span> · {kind} · " + _("started {when}", when=started) + "<br>"
            f"<span style='color:#8a909c'>{agent['cwd'] or ''}</span>")
        self.timeline.clear()
        for e in self.store.events(agent_id):
            when = datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
            what = {"tool": e["tool"] or _("tool"), "prompt": _("request"), "done": _("done"), "waiting": _("waiting"),
                    "start": _("started"), "end": _("ended"), "session": _("session")}.get(e["type"], e["type"])
            self.timeline.addItem(QListWidgetItem(f"{when}  {what}  {e['summary'] or ''}"))
        self.timeline.scrollToBottom()
        if self.tabs.currentWidget() is self.conversation:
            self.load_conversation(agent)

    def load_conversation(self, agent: dict) -> None:
        path = agent.get("transcript")
        if agent["kind"] == "subagent" and path:
            path = transcript.subagent_files(path, agent["id"].split("/", 1)[1])[0]
        messages = transcript.tail_messages(path, 40) if path else []
        if not messages:
            self.conversation.setPlainText(_("(no transcript available for this agent)"))
            return
        lines = [f"[{ROLE_LABEL.get(m['role'], m['role'])}] {m['text']}" for m in messages]
        self.conversation.setPlainText("\n\n".join(lines))
        self.conversation.verticalScrollBar().setValue(self.conversation.verticalScrollBar().maximum())

    def show_managed(self) -> None:
        if self.managed_tab is not None:
            self.main_tabs.setCurrentWidget(self.managed_tab)

    # ---- graph -----------------------------------------------------------
    def graph_visible(self) -> bool:
        return self.isVisible() and self.main_tabs.currentIndex() == 1

    def refresh_graph(self) -> None:
        if not self.graph_visible():
            return
        if self.live.isChecked():
            now = time.time()
            self.graph_view.set_graph(build_graph(self.store.all_agents(), self.store.all_events(5000), now), now)
        else:
            self.render_replay()

    def on_hook_event(self, data: dict) -> None:
        """Live event: rebuild the graph and animate the data traveling along the edges."""
        etype = HOOK_TO_TYPE.get(data.get("ev"))
        sid, aid = data.get("sid"), data.get("aid")
        if not etype or not sid or not self.live.isChecked() or not self.graph_visible():
            return
        self.refresh_graph()
        agent_id = f"{sid}/{aid}" if aid else str(sid)
        color = STATUS_COLOR["waiting"] if etype == "waiting" else "#ffffff"
        for a, b in pulses_for(agent_id, etype, data.get("tool"), clip(data.get("detail")), str(sid) if aid else None):
            self.graph_view.pulse(a, b, color)

    def on_live_toggled(self, live: bool) -> None:
        self.play_timer.stop()
        self.play_btn.setText(_("▶ Play"))
        self.slider.setEnabled(not live)
        self.play_btn.setEnabled(not live)
        if live:
            self.time_label.setText(_("live"))
            self.refresh_graph()
            return
        events = self.store.all_events()
        agents = self.store.all_agents()
        stamps = [e["ts"] for e in events] or [a["started_at"] for a in agents] or [time.time()]
        self.replay.update(agents=agents, events=events, t0=min(stamps), t1=max(max(stamps), min(stamps) + 1))
        self.slider.blockSignals(True)
        self.slider.setValue(1000)
        self.slider.blockSignals(False)
        self.replay["cut"] = self.replay["t1"]
        self.render_replay()

    def render_replay(self, animate_from: float | None = None) -> None:
        r = self.replay
        cut = r["t0"] + (r["t1"] - r["t0"]) * self.slider.value() / 1000
        self.graph_view.set_graph(build_graph(r["agents"], r["events"], cut), cut)
        self.time_label.setText(datetime.fromtimestamp(cut).strftime("%d/%m %H:%M:%S"))
        if animate_from is not None and cut > animate_from:   # only while playing: animate the events of the span
            parents = {a["id"]: a["parent_id"] for a in r["agents"]}
            batch = [e for e in r["events"] if animate_from < e["ts"] <= cut][-12:]
            for e in batch:
                for a, b in pulses_for(e["agent_id"], e["type"], e["tool"], e["summary"], parents.get(e["agent_id"])):
                    self.graph_view.pulse(a, b)
        r["cut"] = cut

    def on_slider(self, _value: int) -> None:
        if not self.live.isChecked():
            self.render_replay()

    def toggle_play(self) -> None:
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play_btn.setText(_("▶ Play"))
            return
        if self.slider.value() >= 1000:
            self.slider.setValue(0)
        self.play_btn.setText(_("⏸ Pause"))
        self.play_timer.start(100)

    def play_tick(self) -> None:
        before = self.replay["cut"]
        value = min(1000, self.slider.value() + REPLAY_STEP)
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.render_replay(animate_from=before)
        if value >= 1000:
            self.toggle_play()

    # ---- actions ---------------------------------------------------------
    def copy_resume(self) -> None:
        agent = self.store.get(self.selected) if self.selected else None
        if not agent:
            return
        if sys.platform == "win32":              # PowerShell
            q = launcher.ps_quote
            cmd = f"claude --resume {q(agent['session_id'])}"
            if agent.get("cwd"):
                cmd = f"Set-Location -LiteralPath {q(agent['cwd'])}; {cmd}"
        else:
            cmd = f"claude --resume {shlex.quote(agent['session_id'])}"
            if agent.get("cwd"):
                cmd = f"cd {shlex.quote(agent['cwd'])} && {cmd}"
        QApplication.clipboard().setText(cmd)
        self.hint.setText(_("Command copied."))
        QTimer.singleShot(2500, lambda: self.hint.setText(_(HINT_READONLY)))

    def open_in_terminal(self) -> None:
        agent = self.store.get(self.selected) if self.selected else None
        if agent and agent.get("session_id") and self.on_resume:
            self.on_resume(agent.get("cwd") or "", agent["session_id"], agent.get("transcript") or "")

    def clear_finished(self) -> None:
        self.store.clear_finished()
        self.selected = None
        self.refresh()

    def showEvent(self, e) -> None:
        self.refresh()
        super().showEvent(e)
