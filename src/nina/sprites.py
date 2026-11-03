"""NINA's pixel art, drawn in code: palette, base sprite, frame variants and rendering."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

PALETTE = {
    "K": "#14161a",  # outline
    "B": "#2a2d34",  # outfit
    "b": "#454b57",  # outfit highlight
    "G": "#7ed321",  # lime green
    "g": "#4e9a12",  # dark green
    "S": "#f2c9a0",  # skin
    "W": "#ffffff",  # eye highlight
    "E": "#1b1b1b",  # eye
    "P": "#f08c8c",  # cheek
}

BASE = [
    "......KKKKKKKKKK......",
    "....KKBBBBBBBBBBKK....",
    "...KBBBBBBBBBBBBBBK...",
    "..KBBbBBBBBBBBBBBBBK..",
    "..KGGGGGGGGGGGGGGGGKGG",
    "..KggggggggggggggggK.G",
    "..KBBSSSSSSSSSSSSBBK..",
    "..KBSSWEESSSSWEESSBK..",
    "..KBSSEEESSSSEEESSBK..",
    "..KBSPSSSSSSSSSSPSBK..",
    "..KBBBBBBBBBBBBBBBBK..",
    "...KBBBBBBBBBBBBBBK...",
    "....KKBBBBBBBBBBKK....",
    "...KBBKBBBBBBBBKBBK...",
    "..KBBBKbBBBBBBbKBBBK..",
    "..KSSKGGGGGGGGGGKSSK..",
    "...KK.KBBBBBBBBK.KK...",
    "......KBBBBBBBBK......",
    "......KBBBKKBBBK......",
    "......KBBBKKBBBK......",
    ".....KKKKK..KKKKK.....",
]


def variant(rows: dict) -> list:
    grid = list(BASE)
    for i, row in rows.items():
        grid[i] = row
    return grid


def patch(row: str, cells: dict) -> str:
    """Replace characters of a row by column: patch(row, {6: 'E', 7: 'E'})."""
    chars = list(row)
    for col, ch in cells.items():
        chars[col] = ch
    return "".join(chars)


# Eyes: left eye is on columns 5-9 (6-8 in BASE), right eye on columns 12-16 (13-15 in BASE).
WORK_ROWS = {
    6: patch(BASE[6], {6: "E", 7: "E", 14: "E", 15: "E"}),   # furrowed brows
    7: patch(BASE[7], {6: "E", 13: "E"}),                    # fixed stare, no highlight
}
_WIDE_TOP, _WIDE_MID, _WIDE_LOW = "EEEEE", "EWWWE", "EWEWE"
WAIT_ROWS = {}
for _r, _eye in ((6, _WIDE_TOP), (7, _WIDE_MID), (8, _WIDE_LOW), (9, _WIDE_TOP)):
    _row = BASE[_r]
    for _c0 in (5, 12):
        _row = _row[:_c0] + _eye + _row[_c0 + 5:]
    WAIT_ROWS[_r] = _row

FRAMES = {
    "idle": BASE,
    "blink": variant({
        7: "..KBSSSSSSSSSSSSSSBK..",
        8: "..KBSSEEESSSSEEESSBK..",
    }),
    "happy": variant({
        7: "..KBSSSESSSSSSESSSBK..",
        8: "..KBSSESESSSSESESSBK..",
    }),
    "sleep": variant({
        7: "..KBSSSSSSSSSSSSSSBK..",
        8: "..KBSSEEESSSSEEESSBK..",
        9: "..KBSPSSSSSSSSSSPSBK..",
    }),
    "work": variant(WORK_ROWS),
    "wait": variant(WAIT_ROWS),
}

GRID_W = len(BASE[0])
GRID_H = len(BASE)
for _name, _grid in FRAMES.items():
    assert len(_grid) == GRID_H and all(len(r) == GRID_W for r in _grid), f"invalid frame: {_name}"


def render_frame(grid: list, scale: int) -> QPixmap:
    pm = QPixmap(GRID_W * scale, GRID_H * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch in PALETTE:
                p.fillRect(x * scale, y * scale, scale, scale, QColor(PALETTE[ch]))
    p.end()
    return pm


def export_images(out) -> None:
    """Write the icon (nina.png) and a contact sheet with every frame (nina_frames.png)."""
    from pathlib import Path
    out = Path(out)
    render_frame(FRAMES["idle"], 8).save(str(out / "nina.png"))
    s = 8
    sheet = QPixmap((GRID_W * s + 16) * len(FRAMES), GRID_H * s + 16)
    sheet.fill(QColor("#e9ecef"))
    p = QPainter(sheet)
    for i, g in enumerate(FRAMES.values()):
        p.drawPixmap(8 + i * (GRID_W * s + 16), 8, render_frame(g, s))
    p.end()
    sheet.save(str(out / "nina_frames.png"))
