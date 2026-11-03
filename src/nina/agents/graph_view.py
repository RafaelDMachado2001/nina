"""
Agent graph view (Obsidian style): nodes that settle by forces, pulses that show the data flow when an
event happens, draggable nodes, background panning and mouse-wheel zoom.
It only draws: the model and the layout live in nina/agents/graph.py.
"""
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from nina.agents.graph import HUB, ForceLayout
from nina.i18n import N_, _

BG = "#14161a"
STATUS_COLOR = {"working": "#7ed321", "waiting": "#ffd400", "done": "#6fb3ff", "idle": "#8a909c",
                "ended": "#5b616e", "stale": "#f08c4a"}
KIND_COLOR = {"hub": "#7ed321", "tool": "#9aa5b8", "mcp": "#b48cff", "read": "#c4a98a", "write": "#f2c9a0",
              "session": "#7ed321", "spawn": "#7ed321"}
STATUS_LABEL = {"working": N_("working"), "waiting": N_("waiting for you"), "done": N_("finished its work"),
                "idle": N_("idle"), "ended": N_("session ended"), "stale": N_("no signal")}
PULSE_S = 0.9


def node_radius(nid: str, node: dict) -> float:
    kind = node["kind"]
    if kind == "hub":
        return 17.0
    if kind == "session":
        return 12.0
    if kind == "subagent":
        return 9.0
    if kind == "file":
        return 5.0
    return 5.0 + min(math.log2(1 + node.get("count", 0)), 4.0)   # tools grow with use


class GraphView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 320)
        self.setMouseTracking(True)
        self.nodes, self.edges = {}, {}
        self.layout = ForceLayout()
        self.ref_time = time.time()
        self.zoom, self.offset = 1.0, QPointF(0, 0)
        self.autofit = True      # frames the graph by itself until you touch the zoom or the background
        self.hover = None
        self.drag_node = None
        self.drag_bg = None
        self.pulses = []
        self.simulating = True
        self.temp = 1.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    # ---- data ------------------------------------------------------------
    def set_graph(self, graph: dict, ref_time: float | None = None) -> None:
        self.nodes, self.edges = graph["nodes"], graph["edges"]
        self.ref_time = time.time() if ref_time is None else ref_time
        if self.layout.update(self.nodes, self.edges):
            self.simulating, self.temp = True, 1.0
        self.update()

    def pulse(self, a: str, b: str, color: str | None = None) -> None:
        if a in self.layout.pos and b in self.layout.pos and len(self.pulses) < 60:
            self.pulses.append({"a": a, "b": b, "t0": time.monotonic(), "color": color or "#ffffff"})

    # ---- animation -------------------------------------------------------
    def showEvent(self, e) -> None:
        self.timer.start(33)
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self.timer.stop()
        super().hideEvent(e)

    def fit(self) -> bool:
        """Ease the zoom and offset toward a framing that shows the whole graph."""
        pos = self.layout.pos
        if not self.autofit or len(pos) < 2:
            return False
        xs, ys = [p[0] for p in pos.values()], [p[1] for p in pos.values()]
        bw, bh = max(xs) - min(xs) + 200, max(ys) - min(ys) + 140
        zoom = max(0.3, min(1.4, min(self.width() / bw, self.height() / bh)))
        target = QPointF(-(max(xs) + min(xs)) / 2 * zoom, -(max(ys) + min(ys)) / 2 * zoom)
        dz, doff = zoom - self.zoom, target - self.offset
        if abs(dz) < 0.002 and abs(doff.x()) + abs(doff.y()) < 0.5:
            return False
        self.zoom += dz * 0.15
        self.offset = self.offset + doff * 0.15
        return True

    def tick(self) -> None:
        now = time.monotonic()
        before = len(self.pulses)
        self.pulses = [p for p in self.pulses if now - p["t0"] < PULSE_S]
        moved = False
        if self.simulating:
            energy = self.layout.step(self.edges, self.temp)
            self.temp = max(0.25, self.temp * 0.99)
            moved = True
            if energy < 0.02 * max(1, len(self.layout.pos)) and self.drag_node is None:
                self.simulating = False
        refit = self.fit()
        if moved or refit or self.pulses or before:
            self.update()

    # ---- coordenadas -----------------------------------------------------
    def to_screen(self, x: float, y: float) -> QPointF:
        return QPointF(self.width() / 2 + self.offset.x() + x * self.zoom,
                       self.height() / 2 + self.offset.y() + y * self.zoom)

    def to_world(self, pt: QPointF) -> tuple:
        return ((pt.x() - self.width() / 2 - self.offset.x()) / self.zoom,
                (pt.y() - self.height() / 2 - self.offset.y()) / self.zoom)

    def node_at(self, pt: QPointF) -> str | None:
        wx, wy = self.to_world(pt)
        best, best_d = None, 1e9
        for nid, (x, y) in self.layout.pos.items():
            node = self.nodes.get(nid)
            if node is None:
                continue
            d = math.hypot(wx - x, wy - y)
            if d <= node_radius(nid, node) + 4 and d < best_d:
                best, best_d = nid, d
        return best

    # ---- desenho ---------------------------------------------------------
    @staticmethod
    def node_color(node: dict) -> QColor:
        if node["kind"] in ("session", "subagent"):
            return QColor(STATUS_COLOR.get(node["status"], "#8a909c"))
        if node["kind"] == "file":
            return QColor(KIND_COLOR["write" if node["status"] == "write" else "read"])
        return QColor(KIND_COLOR.get(node["kind"], "#9aa5b8"))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(BG))
        if len(self.nodes) <= 1:
            p.setPen(QColor("#8a909c"))
            p.drawText(self.rect(), Qt.AlignCenter, _("No agents yet.\nClaude Code sessions show up here."))
            self.draw_legend(p)
            p.end()
            return
        pos = self.layout.pos
        # edges (the most recent ones are livelier)
        for (a, b), edge in self.edges.items():
            if a not in pos or b not in pos:
                continue
            age = max(0.0, self.ref_time - edge["ts"])
            alpha = int(255 * max(0.18, 1 - age / 900))
            color = QColor(KIND_COLOR.get(edge["kind"], "#9aa5b8"))
            color.setAlpha(alpha)
            p.setPen(QPen(color, min(1.0 + 0.5 * math.log2(1 + edge["count"]), 4.0) * min(self.zoom, 1.5)))
            p.drawLine(self.to_screen(*pos[a]), self.to_screen(*pos[b]))
        # pulses: the "data" traveling along the edge
        now = time.monotonic()
        for pu in self.pulses:
            if pu["a"] not in pos or pu["b"] not in pos:
                continue
            t = (now - pu["t0"]) / PULSE_S
            (ax, ay), (bx, by) = pos[pu["a"]], pos[pu["b"]]
            pt = self.to_screen(ax + (bx - ax) * t, ay + (by - ay) * t)
            glow = QColor(pu["color"])
            glow.setAlpha(int(200 * (1 - t) + 40))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(pt, 6.0 * self.zoom * (1 - 0.4 * t), 6.0 * self.zoom * (1 - 0.4 * t))
        # nodes
        few_files = sum(1 for n in self.nodes.values() if n["kind"] == "file") <= 12
        p.setFont(QFont("Sans", 8))
        fm = QFontMetrics(p.font())
        for nid, node in self.nodes.items():
            if nid not in pos:
                continue
            c = self.to_screen(*pos[nid])
            r = node_radius(nid, node) * self.zoom
            color = self.node_color(node)
            if node["kind"] in ("session", "subagent") and node["status"] == "working":
                halo = QColor(color)
                halo.setAlpha(int(70 + 50 * math.sin(now * 5)))
                p.setPen(Qt.NoPen)
                p.setBrush(halo)
                p.drawEllipse(c, r * 1.7, r * 1.7)
            p.setPen(QPen(QColor(BG), 2))
            p.setBrush(color)
            if node["kind"] in ("tool", "mcp"):
                p.drawRoundedRect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), 2, 2)   # ferramentas: quadradinhos
            else:
                p.drawEllipse(c, r, r)
            if node["kind"] == "hub":
                p.setPen(QColor(BG))
                p.setFont(QFont("Sans", max(6, int(7 * self.zoom)), QFont.Bold))
                p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, "N")
                p.setFont(QFont("Sans", 8))
            show = node["kind"] != "file" or few_files or nid == self.hover
            if show:
                text = fm.elidedText(node["label"], Qt.ElideRight, 150)
                tx, ty = c.x() - fm.horizontalAdvance(text) / 2, c.y() + r + fm.ascent() + 2
                p.setPen(QColor(BG))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    p.drawText(QPointF(tx + dx, ty + dy), text)
                p.setPen(QColor("#e6e8ec") if nid != self.hover else QColor("#ffffff"))
                p.drawText(QPointF(tx, ty), text)
        self.draw_legend(p)
        p.end()

    def draw_legend(self, p: QPainter) -> None:
        items = [(_("working"), STATUS_COLOR["working"], "o"), (_("waiting"), STATUS_COLOR["waiting"], "o"),
                 (_("finished"), STATUS_COLOR["done"], "o"), (_("tool"), KIND_COLOR["tool"], "s"),
                 ("MCP", KIND_COLOR["mcp"], "s"), (_("file read"), KIND_COLOR["read"], "o"),
                 (_("file edited"), KIND_COLOR["write"], "o")]
        p.setFont(QFont("Sans", 8))
        x, y = 10, self.height() - 10
        for label, color, shape in reversed(items):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            if shape == "s":
                p.drawRect(QRectF(x, y - 8, 8, 8))
            else:
                p.drawEllipse(QRectF(x, y - 8, 8, 8))
            p.setPen(QColor("#8a909c"))
            p.drawText(QPointF(x + 13, y), label)
            y -= 15

    # ---- mouse -----------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        pt = e.position()
        if e.button() == Qt.LeftButton:
            nid = self.node_at(pt)
            if nid is not None and nid != HUB:
                self.drag_node = nid
                self.layout.pinned.add(nid)
            else:
                self.drag_bg = (pt, QPointF(self.offset))
                self.autofit = False

    def mouseMoveEvent(self, e) -> None:
        pt = e.position()
        if self.drag_node is not None:
            self.layout.pos[self.drag_node] = list(self.to_world(pt))
            self.simulating, self.temp = True, max(self.temp, 0.6)
            self.update()
        elif self.drag_bg is not None:
            start, off0 = self.drag_bg
            self.offset = off0 + (pt - start)
            self.update()
        else:
            nid = self.node_at(pt)
            if nid != self.hover:
                self.hover = nid
                if nid is not None:
                    node = self.nodes[nid]
                    state = _(STATUS_LABEL.get(node["status"], ""))
                    tip = node.get("path") or node["label"]
                    extra = f"\n{state}" if state else ""
                    count = "\n" + _("{n} event(s)", n=node["count"]) if node.get("count") else ""
                    QToolTip.showText(e.globalPosition().toPoint(), f"{tip}{extra}{count}", self)
                else:
                    QToolTip.hideText()
                self.setCursor(Qt.PointingHandCursor if nid else Qt.ArrowCursor)
                self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self.drag_node is not None:
            self.layout.pinned.discard(self.drag_node)
        self.drag_node = None
        self.drag_bg = None

    def mouseDoubleClickEvent(self, _e) -> None:
        self.autofit = True      # double click: frame everything again
        self.update()

    def wheelEvent(self, e) -> None:
        self.autofit = False
        factor = 1.12 if e.angleDelta().y() > 0 else 1 / 1.12
        self.zoom = max(0.3, min(3.0, self.zoom * factor))
        self.update()
