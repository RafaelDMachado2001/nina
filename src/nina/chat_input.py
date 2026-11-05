"""
The chat's message field: `@path` suggestions, images pasted or dropped as attachments, and their thumbnails.
"""
import base64
import os
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QListWidget, QPushButton, QWidget

from nina import mentions
from nina.i18n import _

MAX_IMAGES = 5
MAX_SIDE = 1568           # larger images are scaled down: more pixels cost tokens without adding detail
THUMB = 44
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class Attachment:
    """A pasted or dropped image: `payload` goes to the agent, `thumb` is what the strip shows."""
    def __init__(self, payload: dict, thumb: QPixmap):
        self.payload, self.thumb = payload, thumb


def make_attachment(image: QImage) -> Attachment | None:
    if image.isNull():
        return None
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(MAX_SIDE, MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    thumb = QPixmap.fromImage(image.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return Attachment({"media_type": "image/png", "data": base64.b64encode(bytes(data)).decode("ascii")}, thumb)


def thumbnail_b64(data: str, side: int = 240) -> str:
    """A small PNG (base64) of an attached image, for the conversation log."""
    image = QImage.fromData(QByteArray.fromBase64(data.encode("ascii")))
    if image.isNull():
        return data
    if max(image.width(), image.height()) > side:
        image = image.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QByteArray()
    buf = QBuffer(out)
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return base64.b64encode(bytes(out)).decode("ascii")


def is_image_file(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES and os.path.isfile(path)


class SuggestionPopup(QListWidget):
    """The list of files and folders under the field. It only shows; the field does the keyboard handling."""
    shown = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("Suggestions")
        self.setFocusPolicy(Qt.NoFocus)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.items = []
        self.hide()

    def show_items(self, items: list) -> None:
        self.items = items
        self.clear()
        for s in items:
            self.addItem(s.value)
        if items:
            self.setCurrentRow(0)
            self.setFixedHeight(min(len(items), 8) * 28 + 4)
        self.setVisible(bool(items))
        if items:
            self.shown.emit()
            self.raise_()

    def step(self, delta: int) -> None:
        if self.items:
            self.setCurrentRow((self.currentRow() + delta) % len(self.items))

    def chosen(self):
        row = self.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None


class MentionInput(QLineEdit):
    image_pasted = Signal(object)         # QImage
    images_dropped = Signal(list)         # image file paths (copied in a file manager)

    def __init__(self):
        super().__init__()
        self.popup = None                 # set by the window: a SuggestionPopup
        self.cwd = lambda: os.getcwd()    # set by the window: the chat's folder
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.refresh_popup)
        self.dismissed = False
        self.textChanged.connect(self.on_edited)
        self.cursorPositionChanged.connect(lambda *_a: self.timer.start())

    def on_edited(self, _text: str) -> None:
        self.dismissed = False
        self.timer.start()

    def popup_open(self) -> bool:
        return self.popup is not None and self.popup.isVisible()

    def refresh_popup(self) -> None:
        if self.popup is None:
            return
        found = None if self.dismissed or not self.isEnabled() else mentions.active_mention(self.text(), self.cursorPosition())
        items = mentions.suggest(found[1], self.cwd()) if found else []
        self.popup.show_items(items)

    def accept_suggestion(self) -> bool:
        choice = self.popup.chosen() if self.popup_open() else None
        found = mentions.active_mention(self.text(), self.cursorPosition())
        if choice is None or found is None:
            return False
        start, _query = found
        if choice.is_dir:
            token = f'@"{choice.value}' if " " in choice.value else f"@{choice.value}"
        else:
            token = mentions.format_mention(choice.value) + " "
        text, cursor = self.text(), self.cursorPosition()
        self.setText(text[:start] + token + text[cursor:])
        self.setCursorPosition(start + len(token))
        return True

    def event(self, e) -> bool:
        if e.type() == QEvent.KeyPress and e.key() == Qt.Key_Tab and self.popup_open() and self.accept_suggestion():
            return True
        return super().event(e)

    def keyPressEvent(self, e) -> None:
        if self.popup_open():
            key = e.key()
            if key in (Qt.Key_Up, Qt.Key_Down):
                self.popup.step(-1 if key == Qt.Key_Up else 1)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter) and self.accept_suggestion():
                return
            if key == Qt.Key_Escape:
                self.dismissed = True
                self.popup.hide()
                return
        if e.matches(QKeySequence.Paste) and self.paste_media():
            return
        super().keyPressEvent(e)

    def paste_media(self) -> bool:
        """Ctrl+V with an image (or image files) on the clipboard attaches it; anything with text pastes as text."""
        from PySide6.QtWidgets import QApplication
        mime = QApplication.clipboard().mimeData()
        if mime is None or mime.hasText() and not mime.hasUrls():
            return False
        if mime.hasUrls():
            paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            if paths and all(is_image_file(p) for p in paths):
                self.images_dropped.emit(paths)
                return True
            return False
        if mime.hasImage():
            image = QApplication.clipboard().image()
            if not image.isNull():
                self.image_pasted.emit(image)
                return True
        return False


class AttachmentStrip(QWidget):
    """Thumbnails of the attached images; clicking one removes it."""
    removed = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("Attachments")
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(6)
        self.layout_.addStretch(1)
        self.hide()

    def set_attachments(self, attachments: list) -> None:
        while self.layout_.count() > 1:
            item = self.layout_.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for i, a in enumerate(attachments):
            pm = QPixmap(a.thumb)
            p = QPainter(pm)
            p.fillRect(pm.width() - 12, 0, 12, 12, Qt.black)
            p.setPen(Qt.white)
            p.drawText(pm.width() - 12, 0, 12, 12, Qt.AlignCenter, "×")
            p.end()
            b = QPushButton()
            b.setObjectName("Thumb")
            b.setIcon(QIcon(pm))
            b.setIconSize(pm.size())
            b.setFixedSize(pm.size() + QSize(2, 2))
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(_("Remove image"))
            b.clicked.connect(lambda _c=False, i=i: self.removed.emit(i))
            self.layout_.insertWidget(i, b)
        self.setVisible(bool(attachments))


def place_popup(popup: QWidget, window: QWidget, anchor: QWidget) -> None:
    """Put the list just above `anchor`, as wide as the window."""
    top = anchor.mapTo(window, QPoint(0, 0)).y()
    popup.setFixedWidth(window.width() - 24)
    popup.move(12, max(0, top - popup.height() - 2))
