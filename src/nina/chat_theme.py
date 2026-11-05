"""
Look of the chat window: design tokens (dark and light), the Qt style sheet and the HTML of each kind of message.

Everything is sharp-cornered on purpose (radius 0 matches the pixel-art and is what QTextBrowser draws reliably).
The message HTML sticks to the subset QTextBrowser understands: one-cell tables with a background, colours and sizes.
Standard library only, so it can be tested without Qt.
"""
import html
import re

FONT_UI = '"Archivo", "Noto Sans", "DejaVu Sans"'
FONT_MONO = "'DejaVu Sans Mono'"

THEMES = {
    "dark": dict(
        bg="#101216", surf="#171a20", surf2="#1f232b", surf3="#282d37", border="#2c313b", border_strong="#3b414d",
        text="#e8eaee", text2="#9aa1ad",
        accent="#7ed321", accent_hover="#93e23c", accent_pressed="#68b417", on_accent="#101216", accent_text="#8fdc3a",
        user_bg="#262448", user_text="#ecebff", indigo="#9d90ff",
        warn="#ffd400", warn_bg="#2a2410", warn_text="#ffd84d",
        err_bg="#2c1a1d", err_text="#ff8f8f", info_bg="#12252b", info_text="#6fd0e6",
        code_bg="#0b0d10", code_text="#d7dbe2"),
    "light": dict(
        bg="#f4f5f1", surf="#ffffff", surf2="#ebede7", surf3="#e0e3dc", border="#d5d9cf", border_strong="#b4b9ad",
        text="#15171b", text2="#565c66",
        accent="#7ed321", accent_hover="#8ad92f", accent_pressed="#6fbf17", on_accent="#15171b", accent_text="#3d7a0c",
        user_bg="#e8e5ff", user_text="#15171b", indigo="#5546c9",
        warn="#d9b000", warn_bg="#fff4c2", warn_text="#6b5200",
        err_bg="#fde7e7", err_text="#a3242b", info_bg="#e0f3f7", info_text="#0d5a6a",
        code_bg="#ebede7", code_text="#15171b"),
}

# (foreground, background) pairs that carry text and must reach WCAG AA (4.5:1) in both themes.
TEXT_PAIRS = [
    ("text", "bg"), ("text", "surf"), ("text", "surf3"), ("text2", "bg"), ("text2", "surf"), ("text2", "surf2"),
    ("text2", "surf3"), ("accent_text", "bg"), ("on_accent", "accent"), ("on_accent", "accent_pressed"),
    ("user_text", "user_bg"), ("indigo", "bg"), ("warn_text", "warn_bg"), ("err_text", "err_bg"),
    ("info_text", "info_bg"), ("code_text", "code_bg"), ("text", "surf2"),
]


def tokens(name: str) -> dict:
    return THEMES.get(name) or THEMES["dark"]


def _luminance(hex_color: str) -> float:
    v = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    v = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in v]
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]


def contrast(a: str, b: str) -> float:
    x, y = _luminance(a), _luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def qss(name: str) -> str:
    t = tokens(name)
    return f"""
QWidget#ChatWindow {{
  background: {t['bg']}; color: {t['text']};
  font-family: {FONT_UI}; font-size: 14px;
  border: 1px solid {t['border_strong']};
}}
QWidget#Header {{ background: {t['surf']}; border-bottom: 2px solid {t['border']}; min-height: 38px; max-height: 38px; }}
QLabel {{ background: transparent; color: {t['text']}; }}
QLabel#NameLabel {{ font-size: 14px; font-weight: 700; color: {t['text']}; }}
QLabel#StatusLabel {{ font-size: 12px; font-weight: 500; color: {t['text2']}; }}
QLabel#Hello {{ font-size: 18px; font-weight: 700; color: {t['text']}; }}
QLabel#HelloSub, QLabel#PermWhere, QLabel#ThinkingLabel {{ font-size: 12px; color: {t['text2']}; }}
QLabel#HelloSub {{ font-size: 13px; }}
QLabel#PermTitle {{ font-size: 14px; font-weight: 700; color: {t['text']}; }}
QLabel#PermCommand {{
  background: {t['code_bg']}; color: {t['code_text']}; padding: 8px 10px;
  font-family: {FONT_MONO}; font-size: 12px;
}}
QWidget#Composer {{ background: {t['surf']}; border-top: 2px solid {t['border']}; }}
QWidget#EmptyState, QWidget#Thinking {{ background: {t['bg']}; }}

QLineEdit {{
  background: {t['bg']}; color: {t['text']};
  border: 1px solid {t['border_strong']}; border-radius: 0;
  padding: 0 10px; min-height: 34px; font-size: 14px;
  selection-background-color: {t['accent']}; selection-color: {t['on_accent']};
}}
QLineEdit:hover {{ border-color: {t['text2']}; }}
QLineEdit:focus {{ border: 2px solid {t['accent']}; padding: 0 9px; }}
QLineEdit:disabled {{ background: {t['surf']}; color: {t['text2']}; border-color: {t['border']}; }}

QPushButton {{
  background: {t['surf2']}; color: {t['text']};
  border: 1px solid {t['border_strong']}; border-radius: 0;
  padding: 0 12px; min-height: 34px; min-width: 64px;
  font-size: 13px; font-weight: 600; text-align: left;
}}
QPushButton:hover {{ background: {t['surf3']}; }}
QPushButton:pressed {{ background: {t['border']}; }}
QPushButton:focus {{ border: 2px solid {t['accent']}; padding: 0 11px; }}
QPushButton:disabled {{ background: {t['surf']}; color: {t['text2']}; border-color: {t['border']}; }}

QPushButton#Send {{
  background: {t['accent']}; color: {t['on_accent']}; border: none;
  min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0;
}}
QPushButton#Send:hover {{ background: {t['accent_hover']}; }}
QPushButton#Send:pressed {{ background: {t['accent_pressed']}; }}
QPushButton#Send:focus {{ border: 2px solid {t['text']}; }}
QPushButton#Send:disabled {{ background: {t['surf3']}; color: {t['text2']}; }}

QPushButton#Interrupt, QPushButton#Allow {{ background: transparent; }}
QPushButton#Interrupt:hover, QPushButton#Allow:hover {{ background: {t['surf3']}; }}
QPushButton#Interrupt:pressed, QPushButton#Allow:pressed {{ background: {t['border']}; }}
QPushButton#Deny {{ background: {t['surf3']}; border: 2px solid {t['accent']}; padding: 0 11px; min-width: 112px; }}

QPushButton#Suggestion {{
  background: {t['surf']}; border: 1px solid {t['border']};
  font-weight: 500; min-height: 34px; text-align: left; padding: 0 12px;
}}
QPushButton#Suggestion:hover {{ background: {t['surf3']}; }}

QPushButton#IconButton {{
  background: transparent; border: none; padding: 0; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
}}
QPushButton#IconButton:hover {{ background: {t['surf3']}; }}
QPushButton#IconButton:pressed {{ background: {t['border']}; }}
QPushButton#IconButton:focus {{ border: 2px solid {t['accent']}; }}

QPushButton#AccountChip {{
  background: transparent; color: {t['text2']}; border: 1px solid {t['border']}; padding: 0 8px;
  min-width: 0; max-width: 110px; min-height: 22px; max-height: 22px; font-size: 11px; font-weight: 600;
}}
QPushButton#AccountChip:hover {{ background: {t['surf3']}; color: {t['text']}; }}
QPushButton#AccountChip:disabled {{ color: {t['text2']}; border-color: transparent; }}
QPushButton#AccountChip:focus {{ border: 1px solid {t['accent']}; padding: 0 8px; }}

QWidget#TabRow {{ background: {t['surf']}; border-bottom: 1px solid {t['border']}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
  background: transparent; color: {t['text2']}; border: none; border-bottom: 2px solid transparent;
  padding: 0 2px 0 10px; min-height: 30px; max-width: 150px; font-size: 12px; font-weight: 600;
}}
QTabBar::tab:hover {{ background: {t['surf3']}; color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}
QPushButton#TabClose {{
  background: transparent; border: none; padding: 0; min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px;
}}
QPushButton#TabClose:hover {{ background: {t['border']}; }}

QWidget#PermissionSheet {{ background: {t['surf']}; border-top: 2px solid {t['warn']}; }}

QTextBrowser {{
  background: {t['bg']}; color: {t['text']}; border: none; padding: 0;
  selection-background-color: {t['accent']}; selection-color: {t['on_accent']};
}}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['border_strong']}; min-height: 32px; margin: 0 2px 0 0; }}
QScrollBar::handle:vertical:hover {{ background: {t['text2']}; }}
QScrollBar::handle:vertical:pressed {{ background: {t['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {t['border_strong']}; min-width: 32px; margin: 0 0 2px 0; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; border: none; }}

QMenu {{
  background: {t['surf2']}; color: {t['text']};
  border: 1px solid {t['border_strong']}; padding: 4px 0; font-size: 13px;
}}
QMenu::item {{ padding: 8px 16px 8px 12px; min-height: 16px; }}
QMenu::item:selected {{ background: {t['surf3']}; color: {t['text']}; }}
QMenu::item:disabled {{ color: {t['text2']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 0; }}
QMenu::shortcut {{ color: {t['text2']}; }}

QToolTip {{ background: {t['surf2']}; color: {t['text']}; border: 1px solid {t['border_strong']}; padding: 4px 8px; }}
"""


# ---- message HTML (QTextBrowser subset) -------------------------------------------------------------

def _inline(text: str, t: dict) -> str:
    body = html.escape(text)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
    body = re.sub(r"`([^`\n]+)`", lambda m: f"<span style=\"font-family: {FONT_MONO}; font-size: 13px; "
                                           f"background-color: {t['surf2']};\">&nbsp;{m.group(1)}&nbsp;</span>", body)
    return body.replace("\n", "<br>")


def _code_block(text: str, t: dict) -> str:
    return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin: 0 0 10px 0;"><tr>'
            f'<td bgcolor="{t["code_bg"]}" style="padding: 8px 10px;"><pre style="margin: 0; font-family: {FONT_MONO}; '
            f'font-size: 12px; color: {t["code_text"]};">{html.escape(text.strip(chr(10)))}</pre></td></tr></table>')


def assistant_html(text: str, name: str = "dark") -> str:
    """NINA's text: running prose (no bubble) with **bold**, `inline code` and ``` fenced blocks ```."""
    t = tokens(name)
    parts = re.split(r"```[^\n]*\n(.*?)```", text, flags=re.S)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(_code_block(part, t))
        elif part.strip():
            out.append(f'<p style="margin: 0 0 10px 0; line-height: 150%; color: {t["text"]}; font-size: 14px;">'
                       f'{_inline(part.strip(chr(10)), t)}</p>')
    return "".join(out)


def user_html(text: str, name: str = "dark") -> str:
    t = tokens(name)
    return (f'<table align="right" cellspacing="0" cellpadding="0" style="margin: 0 0 10px 48px;"><tr>'
            f'<td bgcolor="{t["user_bg"]}" style="padding: 8px 10px; color: {t["user_text"]}; font-size: 14px;">'
            f'{html.escape(text).replace(chr(10), "<br>")}</td></tr></table>')


def tool_html(label: str, name: str = "dark") -> str:
    """`label` is "Tool: argument"."""
    t = tokens(name)
    tool, _sep, arg = label.partition(": ")
    return (f'<p style="margin: 0 0 6px 0; font-family: {FONT_MONO}; font-size: 12px; color: {t["text2"]};">'
            f'<span style="color: {t["accent_text"]};">&rsaquo;</span> <b>{html.escape(tool)}</b> {html.escape(arg)}</p>')


def info_html(text: str, name: str = "dark") -> str:
    t = tokens(name)
    return (f'<table cellspacing="0" cellpadding="0" style="margin: 0 0 10px 0;"><tr>'
            f'<td bgcolor="{t["info_bg"]}" style="padding: 3px 8px; color: {t["info_text"]}; font-size: 12px; '
            f'font-weight: 500;">{html.escape(text)}</td></tr></table>')


def notice_html(kind: str, text: str, name: str = "dark", link: tuple | None = None) -> str:
    """Blocked action (kind "blocked", amber) or error (kind "error", red). `link` is (label, href)."""
    t = tokens(name)
    bg, fg = (t["warn_bg"], t["warn_text"]) if kind == "blocked" else (t["err_bg"], t["err_text"])
    label, _sep, rest = text.partition(": ")
    head = f"<b>{html.escape(label)}:</b> {html.escape(rest)}" if rest else html.escape(text)
    tail = f' <a href="{html.escape(link[1])}" style="color: {fg}; font-weight: 600;">{html.escape(link[0])}</a>' if link else ""
    return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin: 0 0 10px 0;"><tr>'
            f'<td bgcolor="{bg}" style="padding: 6px 10px; color: {fg}; font-size: 13px;">{head}{tail}</td></tr></table>')
