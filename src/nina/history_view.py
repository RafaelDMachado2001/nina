"""The history list shown inside the chat window: search, resume and delete."""
import html as html_lib
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from nina import chathistory, chatinfo
from nina.i18n import _


class HistoryView(QWidget):
    resume = Signal(dict)            # the record to open in a tab
    back = Signal()
    deleted = Signal(str)

    def __init__(self, base: Path | None = None):
        super().__init__()
        self.base = base
        self.setObjectName("HistoryView")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.records = []
        self.open_ids = set()
        self.tokens = None
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("Search by title, account or folder..."))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.fill)
        self.list = QListWidget()
        self.list.setObjectName("HistoryList")
        self.list.setFocusPolicy(Qt.StrongFocus)
        self.list.itemDoubleClicked.connect(lambda _i: self.resume_selected())
        self.list.itemActivated.connect(lambda _i: self.resume_selected())
        self.list.currentRowChanged.connect(lambda _r: self.update_buttons())
        self.empty = QLabel("")
        self.empty.setObjectName("HelloSub")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.summary = QLabel("")
        self.summary.setObjectName("HelloSub")
        self.resume_btn = QPushButton(_("Resume"))
        self.resume_btn.setObjectName("Deny")             # the primary button style of the window
        self.resume_btn.clicked.connect(self.resume_selected)
        self.delete_btn = QPushButton(_("Delete"))
        self.delete_btn.clicked.connect(self.delete_selected)
        self.back_btn = QPushButton(_("Back to chat"))
        self.back_btn.clicked.connect(self.back.emit)
        self.confirm = QTimer(self)
        self.confirm.setSingleShot(True)
        self.confirm.timeout.connect(self.update_buttons)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.resume_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.back_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(self.search)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.empty)
        lay.addWidget(self.summary)
        lay.addLayout(buttons)

    # ---- content -----------------------------------------------------------------------------------
    def refresh(self, open_ids: set, tokens: dict) -> None:
        self.open_ids = set(open_ids)
        self.tokens = tokens
        self.records = chathistory.list_meta(self.base)
        n, size = chathistory.usage(self.base)
        self.summary.setText(_("{n} conversations · {size} on disk", n=n, size=chatinfo.size_text(size)))
        self.search.clear()
        self.fill()
        self.search.setFocus()

    def fill(self) -> None:
        t = self.tokens or {"text": "#fff", "text2": "#999", "accent": "#7ed321"}
        query = self.search.text()
        self.list.clear()
        shown = [r for r in self.records if chathistory.matches(r, query)]
        for rec in shown:
            when = datetime.fromtimestamp(rec["updated"] or rec["created"])
            d, tm = chatinfo.clock_text(when)
            parts = [f"{d} {tm}", rec["account"] or _("default"), Path(rec["cwd"]).name or rec["cwd"],
                     _("{n} messages", n=rec["count"])]
            if rec["id"] in self.open_ids:
                parts.append(_("open"))
            title = rec["title"] or _("New chat")
            label = QLabel(f'<div style="font-weight:600; color:{t["text"]}">{html_lib.escape(title)}</div>'
                           f'<div style="font-size:11px; color:{t["text2"]}">{html_lib.escape(" · ".join(parts))}</div>')
            label.setTextFormat(Qt.RichText)
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            label.setContentsMargins(8, 4, 8, 4)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, rec["id"])
            item.setSizeHint(label.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, label)
        if shown:
            self.list.setCurrentRow(0)
        self.empty.setText("" if shown else (_("No conversations match.") if query.strip() else
                                             _("No saved conversations yet. They are saved as you talk.")))
        self.empty.setVisible(not shown)
        self.update_buttons()

    def selected(self) -> dict | None:
        item = self.list.currentItem()
        conv = item.data(Qt.UserRole) if item else None
        return next((r for r in self.records if r["id"] == conv), None)

    def update_buttons(self) -> None:
        rec = self.selected()
        self.resume_btn.setEnabled(rec is not None)
        self.delete_btn.setEnabled(rec is not None and rec["id"] not in self.open_ids)   # close an open chat first
        self.delete_btn.setText(_("Delete"))
        self.resume_btn.setText(_("Open") if rec and rec["id"] in self.open_ids else _("Resume"))

    # ---- actions -----------------------------------------------------------------------------------
    def resume_selected(self) -> None:
        rec = self.selected()
        if rec is not None:
            self.resume.emit(rec)

    def delete_selected(self) -> None:
        """Two steps instead of a dialog: the first click asks, a second one within 4 s deletes."""
        rec = self.selected()
        if rec is None or rec["id"] in self.open_ids:
            return
        if not self.confirm.isActive():
            self.delete_btn.setText(_("Click again to delete"))
            self.confirm.start(4000)
            return
        self.confirm.stop()
        chathistory.delete(rec["id"], self.base)
        self.records = [r for r in self.records if r["id"] != rec["id"]]
        self.deleted.emit(rec["id"])
        self.fill()
