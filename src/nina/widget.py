"""The mascot: a frameless, always-on-top pixel-art widget that reacts to Claude Code and opens it with a click."""
import json
import math
import random
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QProcess, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QWidget

from nina import autoapprove, claude_hooks, config, connections, launcher, plugins
from nina.config import APP_NAME
from nina.i18n import N_, _
from nina.sprites import FRAMES, GRID_H, GRID_W, PALETTE, render_frame

try:   # the agents panel is optional: if it fails to import, the rest of NINA keeps working
    from nina.agents.store import AgentStore
except Exception:   # pragma: no cover
    AgentStore = None

try:   # managed agents need the optional claude-agent-sdk
    from nina import chat
    from nina.agents import managed, managed_view
except Exception:   # pragma: no cover
    chat = managed = managed_view = None

WORK_TIMEOUT_S = 180   # no events for this long and "working" goes back to normal (e.g. you pressed Esc)

TOOL_LABELS = {
    "Edit": N_("Editing..."), "Write": N_("Editing..."), "MultiEdit": N_("Editing..."),
    "NotebookEdit": N_("Editing..."),
    "Bash": N_("Running a command..."),
    "Read": N_("Reading..."),
    "Grep": N_("Searching..."), "Glob": N_("Searching..."),
    "WebSearch": N_("Searching..."), "WebFetch": N_("Searching..."),
}

PHRASES = [N_("Let's code!"), N_("Here we go!"), N_("Ninja on!"), N_("Full speed!"),
           N_("Opening Claude..."), N_("Mission accepted!")]


class Nina(QWidget):
    TICK_MS = 120
    BUBBLE_H = 34

    def __init__(self):
        super().__init__()
        self.cfg = config.load_config()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.chat_box = None
        self.setToolTip(_("Double click: talk to NINA\nRight click: options")
                        if self.managed_available() else _("Click: open Claude Code\nRight click: options"))

        self.t = 0
        self.blink_ticks = 0
        self.next_blink = random.randint(15, 40)
        self.happy_ticks = 0
        self.jump = []
        self.bubble_text = ""
        self.bubble_ticks = 0
        self.sleeping = False
        self.sessions = {}      # session_id (or "alert:<key>") -> {"state", "last", "tool", ...}
        self.processes = set()  # running QProcess objects (see run_process)
        self.agents = None
        self.agents_panel = None
        self.agents_active = 0
        if AgentStore is not None:
            try:
                self.agents = AgentStore()
            except Exception as e:   # without the agents panel, everything else still works
                print(f"NINA: agents panel unavailable: {e}", file=sys.stderr)
        self.last_activity = time.monotonic()
        self.press_global = None
        self.press_window = None
        self.dragging = False

        self.apply_scale(self.cfg.get("scale", 4), save=False)
        self.place_initial()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(self.TICK_MS)

        self.plugins = plugins.load_plugins(self)

    def shutdown(self) -> None:
        """Stop the animation and release the agent registry (called when the application quits)."""
        self.timer.stop()
        if self.chat_box is not None:
            self.chat_box.flush()
            for tab in self.chat_box.tabs:
                tab.controller.shutdown()
        if self.agents is not None:
            self.agents.close()

    # ---- layout ----------------------------------------------------------
    def apply_scale(self, scale: int, save: bool = True) -> None:
        self.scale = max(2, min(int(scale), 8))
        self.frames = {name: render_frame(g, self.scale) for name, g in FRAMES.items()}
        self.sprite_w = GRID_W * self.scale
        self.sprite_h = GRID_H * self.scale
        w = max(self.sprite_w + 48, 220)
        h = self.BUBBLE_H + self.sprite_h + 8 * self.scale
        old_bottom_center = self.geometry().center().x(), self.geometry().bottom()
        self.setFixedSize(w, h)
        if save:
            cx, bottom = old_bottom_center
            self.move(cx - w // 2, bottom - h + 1)
            self.cfg["scale"] = self.scale
            self.save_pos()

    def place_initial(self) -> None:
        pos = self.cfg.get("pos")
        if pos and QApplication.screenAt(QPoint(pos[0] + self.width() // 2, pos[1] + self.height() // 2)):
            self.move(pos[0], pos[1])
            return
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    def save_pos(self) -> None:
        self.cfg["pos"] = [self.x(), self.y()]
        config.save_config(self.cfg)

    # ---- animation -------------------------------------------------------
    def tick(self) -> None:
        self.t += 1
        if self.blink_ticks > 0:
            self.blink_ticks -= 1
        else:
            self.next_blink -= 1
            if self.next_blink <= 0:
                self.blink_ticks = 2
                self.next_blink = random.randint(20, 50)
        if self.happy_ticks > 0:
            self.happy_ticks -= 1
        if self.jump:
            self.jump.pop(0)
        elif self.mode() == "wait" and self.t % 8 == 0:
            self.jump = [1, 2, 2, 1]
        if self.bubble_ticks > 0:
            self.bubble_ticks -= 1
        if self.agents is not None and self.t % 8 == 0:
            try:
                self.agents_active = self.agents.active_count()
            except Exception:
                pass
        now = time.monotonic()
        for sid, st in list(self.sessions.items()):
            if st["state"] == "working" and now - st["last"] > WORK_TIMEOUT_S:
                del self.sessions[sid]   # no events (e.g. Esc): back to normal
            elif st.get("kind") == "alert" and now - st["last"] > st.get("ttl", 1200):
                del self.sessions[sid]
        idle_s = now - self.last_activity
        if (not self.sleeping and not self.sessions
                and idle_s > self.cfg.get("sleep_minutes", 10) * 60):
            self.sleeping = True
        self.update()

    def touch(self) -> None:
        self.last_activity = time.monotonic()
        self.sleeping = False

    def say(self, text: str, seconds: float = 2.5) -> None:
        self.bubble_text = text[:60]
        self.bubble_ticks = int(seconds * 1000 / self.TICK_MS)
        self.update()

    def celebrate(self) -> None:
        self.touch()
        self.happy_ticks = 12
        self.jump = [2, 4, 5, 5, 4, 2, 1, 0]

    def mode(self) -> str:
        """Priority between sessions: waiting > working > normal."""
        states = {st["state"] for st in self.sessions.values()}
        if "waiting" in states:
            return "wait"
        if "working" in states:
            return "work"
        return "normal"

    def current_wait_text(self) -> str:
        """Text of the sticky bubble: a Claude permission request wins over a plugin alert."""
        waiting = [st for st in self.sessions.values() if st["state"] == "waiting"]
        for st in reversed(waiting):
            if st.get("kind") != "alert":
                return st.get("text", "")
        return waiting[0].get("text", "") if waiting else ""

    def current_frame(self) -> QPixmap:
        mode = self.mode()
        if self.sleeping:
            return self.frames["sleep"]
        if self.happy_ticks > 0:
            return self.frames["happy"]
        if mode == "wait":
            return self.frames["wait"]
        if mode == "work":
            return self.frames["work"]
        if self.blink_ticks > 0:
            return self.frames["blink"]
        return self.frames["idle"]

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        s = self.scale
        w, h = self.width(), self.height()
        mode = self.mode()
        bob_period = 14 if self.sleeping else 2 if mode == "wait" else 5
        bob = s if (self.t // bob_period) % 2 else 0
        lift = (self.jump[0] * s) if self.jump else 0
        x = (w - self.sprite_w) // 2
        base_y = h - self.sprite_h - 2 * s

        # shadow
        p.setRenderHint(QPainter.Antialiasing)
        shadow_w = self.sprite_w * 0.55 * (1 - lift / (12 * s + 1))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QRectF(w / 2 - shadow_w / 2, h - 3 * s, shadow_w, 2.2 * s))

        # sprite
        p.setRenderHint(QPainter.Antialiasing, False)
        y = base_y - lift + (bob if not lift else 0)
        p.drawPixmap(x, y, self.current_frame())

        p.setRenderHint(QPainter.Antialiasing)
        # Zzz
        if self.sleeping:
            font = QFont("Sans", max(8, 3 * s), QFont.Bold)
            p.setFont(font)
            phase = (self.t // 6) % 3
            for i in range(phase + 1):
                zx = x + self.sprite_w - 2 * s + i * 3 * s
                zy = y + 2 * s - i * 4 * s
                self.outlined_text(p, zx, zy, "z" if i < 2 else "Z")

        # badge with the number of active agents (from 2 up)
        if self.agents_active >= 2:
            p.setPen(QPen(QColor(PALETTE["K"]), 2))
            p.setBrush(QColor(PALETTE["G"]))
            p.drawEllipse(QPointF(x + self.sprite_w - s, y + 2 * s), 3.2 * s, 3.2 * s)
            p.setFont(QFont("Sans", max(8, 3 * s), QFont.Bold))
            p.setPen(QColor(PALETTE["K"]))
            p.drawText(QRectF(x + self.sprite_w - 4.2 * s, y - 1.2 * s, 6.4 * s, 6.4 * s), Qt.AlignCenter,
                       str(self.agents_active))

        # spinning shuriken (working)
        if mode == "work" and not self.sleeping:
            self.draw_shuriken(p, x - 3 * s, y + 11 * s, 3.2 * s, (self.t * 35) % 360)

        # yellow "!" (waiting)
        if mode == "wait" and not self.sleeping and self.happy_ticks == 0:
            self.draw_alert(p, w // 2, y - s, s)

        # speech bubble (the waiting alert stays pinned while there is no other bubble)
        text = self.bubble_text if self.bubble_ticks > 0 else (self.current_wait_text() if mode == "wait" else "")
        if text:
            font = QFont("Sans", 10, QFont.Bold)
            p.setFont(font)
            fm = QFontMetrics(font)
            bw = min(fm.horizontalAdvance(text) + 18, w - 4)
            text = fm.elidedText(text, Qt.ElideRight, int(bw - 18))
            bx = (w - bw) / 2
            rect = QRectF(bx, 2, bw, 24)
            p.setPen(QPen(QColor(PALETTE["K"]), 2))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(rect, 10, 10)
            p.setPen(QColor(PALETTE["K"]))
            p.drawText(rect, Qt.AlignCenter, text)
        p.end()

    @staticmethod
    def draw_shuriken(p: QPainter, cx: float, cy: float, r: float, angle: float) -> None:
        p.save()
        p.translate(cx, cy)
        p.rotate(angle)
        star = QPolygonF()
        for i in range(8):
            rad = r if i % 2 == 0 else r * 0.38
            a = i * math.pi / 4 - math.pi / 2
            star.append(QPointF(rad * math.cos(a), rad * math.sin(a)))
        p.setPen(QPen(QColor(PALETTE["K"]), 1.5))
        p.setBrush(QColor(PALETTE["G"]))
        p.drawPolygon(star)
        p.setBrush(QColor(PALETTE["K"]))
        p.drawEllipse(QPointF(0, 0), r * 0.18, r * 0.18)
        p.restore()

    @staticmethod
    def draw_alert(p: QPainter, cx: int, base: int, s: int) -> None:
        """Yellow pixel-art '!' with an outline, above the head."""
        yellow, dark = QColor("#ffd400"), QColor(PALETTE["K"])
        bar = QRectF(cx - s, base - 6 * s, 2 * s, 3.5 * s)
        dot = QRectF(cx - s, base - 2 * s, 2 * s, 2 * s)
        p.setPen(Qt.NoPen)
        for r in (bar, dot):
            p.setBrush(dark)
            p.drawRect(r.adjusted(-s * 0.5, -s * 0.5, s * 0.5, s * 0.5))
        for r in (bar, dot):
            p.setBrush(yellow)
            p.drawRect(r)

    @staticmethod
    def outlined_text(p: QPainter, x: int, y: int, text: str) -> None:
        p.setPen(QColor(PALETTE["K"]))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(x + dx, y + dy, text)
        p.setPen(QColor(PALETTE["G"]))
        p.drawText(x, y, text)

    # ---- mouse -----------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.press_global = e.globalPosition().toPoint()
            self.press_window = self.pos()
            self.dragging = False

    def mouseMoveEvent(self, e) -> None:
        if self.press_global is None:
            return
        delta = e.globalPosition().toPoint() - self.press_global
        if not self.dragging and delta.manhattanLength() > 5:
            self.dragging = True
        if self.dragging:
            self.move(self.press_window + delta)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.LeftButton or self.press_global is None:
            return
        if self.dragging:
            self.save_pos()
            self.touch()
        elif self.mode() == "wait":
            self.dismiss_waiting()
        elif not self.managed_available():     # with the chat available, only a double click does something
            self.launch(config.default_dir_of(self.cfg))
        self.press_global = None
        self.dragging = False

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self.managed_available():
            self.toggle_chat()

    def enterEvent(self, _e) -> None:
        if self.sleeping:
            return
        self.last_activity = time.monotonic()

    # ---- actions ---------------------------------------------------------
    def dismiss_waiting(self) -> None:
        """Only dismisses the alert; it never opens another terminal."""
        claude_waiting = any(st["state"] == "waiting" and st.get("kind") != "alert" for st in self.sessions.values())
        if claude_waiting:
            self.sessions = {sid: st for sid, st in self.sessions.items()
                             if st["state"] != "waiting" or st.get("kind") == "alert"}
            self.touch()
            self.say(_("Go to the terminal!"), 3)
            return
        alerts = [st for st in self.sessions.values() if st.get("kind") == "alert"]
        self.sessions = {sid: st for sid, st in self.sessions.items() if st.get("kind") != "alert"}
        self.touch()
        self.say(_("Alert dismissed."), 3)
        for st in alerts:
            if callable(st.get("on_click")):
                try:
                    st["on_click"]()
                except Exception as e:
                    print(f"NINA: alert handler failed: {e}", file=sys.stderr)

    def launch(self, directory: str, account: str | None = None, profile: str | None = None) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_launch", 0) < 3:   # a double click would open two terminals
            return
        self._last_launch = now
        self.celebrate()
        self.say(_(random.choice(PHRASES)))
        QTimer.singleShot(350, lambda: self._do_launch(directory, account, profile))

    def _do_launch(self, directory: str, account: str | None = None, profile: str | None = None) -> None:
        named = (self.cfg.get("profiles") or {}).get(profile) if profile else None
        if isinstance(named, dict):   # a named profile (e.g. "work") may pin the account
            slug, prof = named.get("account") or self.cfg.get("account", ""), named
        else:
            slug = account if account is not None else self.cfg.get("account", "")
            prof = config.profile_of(self.cfg, slug)
        err = launcher.open_claude(directory, self.cfg.get("terminal", ""), slug, prof)
        if err:
            self.say(_("Oops! No terminal :("), 4)
            QMessageBox.warning(self, APP_NAME, err)
            return
        if profile:
            return
        recent = [d for d in self.cfg.get("recent", []) if d != directory]
        self.cfg["recent"] = [directory] + recent[:7]
        config.save_config(self.cfg)

    def resume_in_terminal(self, directory: str, session_id: str, transcript: str = "") -> None:
        """Reopen an observed Claude Code session in a terminal (`claude --resume`), on the account it ran under."""
        slug = launcher.account_of_transcript(transcript) or self.cfg.get("account", "")
        err = launcher.open_claude(directory or config.default_dir_of(self.cfg), self.cfg.get("terminal", ""), slug,
                                   config.profile_of(self.cfg, slug), resume=session_id)
        if err:
            QMessageBox.warning(self, APP_NAME, err)

    def pick_folder(self, title: str) -> str | None:
        d = QFileDialog.getExistingDirectory(self, title, self.cfg["default_dir"])
        return d or None

    def contextMenuEvent(self, e) -> None:
        self.touch()
        self.build_menu().exec(e.globalPos())

    def build_menu(self) -> QMenu:
        menu = QMenu(self)

        if self.managed_available():
            menu.addAction(_("Talk to NINA...")).triggered.connect(lambda: self.toggle_chat())
        if self.agents is not None:
            menu.addAction(_("Agents ({n} active)...", n=self.agents_active)).triggered.connect(self.show_agents)
            if self.managed_available():
                menu.addAction(_("New agent...")).triggered.connect(self.new_agent)
            menu.addSeparator()
        accounts = config.list_accounts()
        slug = self.cfg.get("account", "")
        label = f" [{slug}]" if slug else ""
        act = menu.addAction(_("Open Claude Code{account} in {path}", account=label,
                               path=config.short_path(config.default_dir_of(self.cfg))))
        act.triggered.connect(lambda: self.launch(config.default_dir_of(self.cfg)))

        sub = menu.addMenu(_("Open in..."))
        for d in self.cfg.get("recent", []):
            if Path(d).is_dir():
                a = sub.addAction(config.short_path(d))
                a.triggered.connect(lambda _=False, d=d: self.launch(d))
        if sub.actions():
            sub.addSeparator()
        choose = sub.addAction(_("Choose folder..."))
        choose.triggered.connect(self.choose_and_launch)

        others = [a for a in accounts if a != slug]
        if others:
            other_menu = menu.addMenu(_("Open with another account"))
            for a in others:
                other_menu.addAction(a).triggered.connect(
                    lambda _=False, a=a: self.launch(config.default_dir_of(self.cfg, a), a))

        plugins.build_menus(self.plugins, self, menu)

        set_default = menu.addAction(_("Set default folder..."))
        set_default.triggered.connect(self.choose_default)

        menu.addSeparator()
        size_menu = menu.addMenu(_("Size"))
        group = QActionGroup(size_menu)
        for text, sc in ((_("Small"), 3), (_("Medium"), 4), (_("Large"), 6)):
            a = QAction(text, size_menu, checkable=True, checked=(self.scale == sc))
            a.triggered.connect(lambda _=False, sc=sc: self.apply_scale(sc))
            group.addAction(a)
            size_menu.addAction(a)

        auto = QAction(_("Start with the system"), menu, checkable=True, checked=launcher.autostart_enabled())
        auto.triggered.connect(lambda checked: launcher.set_autostart(checked))
        menu.addAction(auto)

        acct_menu = menu.addMenu(_("Claude account"))
        acct_group = QActionGroup(acct_menu)
        if accounts:
            for a in accounts:
                act_a = QAction(a, acct_menu, checkable=True, checked=(a == slug))
                act_a.triggered.connect(lambda _=False, a=a: self.set_account(a))
                acct_group.addAction(act_a)
                acct_menu.addAction(act_a)
        else:
            acct_menu.addAction(_("No accounts in {path}", path=config.short_path(str(config.ACCOUNTS_ROOT)))).setEnabled(False)

        menu.addAction(_("Connections and agents...")).triggered.connect(self.open_connections)

        link = QAction(_("Connect to Claude Code"), menu, checkable=True, checked=claude_hooks.hooks_installed())
        link.setToolTip(_("Installs the hooks that make NINA react to what Claude Code is doing"))
        link.triggered.connect(self.toggle_hooks)
        menu.addAction(link)

        nap = menu.addAction(_("Wake up") if self.sleeping else _("Take a nap"))
        nap.triggered.connect(self.toggle_sleep)

        menu.addSeparator()
        menu.addAction(_("Quit")).triggered.connect(QApplication.quit)
        return menu

    def choose_and_launch(self) -> None:
        d = self.pick_folder(_("Open Claude Code in which folder?"))
        if d:
            self.launch(d)

    def choose_default(self) -> None:
        d = self.pick_folder(_("NINA default folder"))
        if d:
            slug = self.cfg.get("account", "")
            if slug:
                self.cfg.setdefault("profiles", {}).setdefault(slug, {})["default_dir"] = d
            else:
                self.cfg["default_dir"] = d
            config.save_config(self.cfg)
            self.say(_("Noted!"))

    def set_account(self, slug: str) -> None:
        self.cfg["account"] = slug
        config.save_config(self.cfg)
        self.say(_("Account: {slug}", slug=slug), 3)
        if not (config.ACCOUNTS_ROOT / slug / ".credentials.json").exists():
            QMessageBox.information(
                self, APP_NAME,
                _("The account '{slug}' has no login yet.\nOpen Claude on it and use /login (or run: claude @{slug}).", slug=slug))

    def toggle_hooks(self, checked: bool) -> None:
        try:
            msg = claude_hooks.install_hooks() if checked else claude_hooks.uninstall_hooks()
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_NAME, _("Could not change {path}:\n{error}", path=config.settings_path(), error=e))
            return
        self.say(_("Connected!") if checked else _("Disconnected."), 3)
        QMessageBox.information(self, APP_NAME, msg)

    def toggle_sleep(self) -> None:
        if self.sleeping:
            self.touch()
            self.say(_("I'm awake!"))
        else:
            self.sleeping = True

    # ---- plugin API (see nina/plugins.py) ----------------------------------
    def set_alert(self, key: str, text: str, count: int = 1, on_click=None, ttl_s: float = 1200) -> None:
        """Raise (or update) a persistent alert: wide eyes, a yellow "!" and a sticky bubble until clicked or expired."""
        self.sessions[f"alert:{key}"] = {"state": "waiting", "kind": "alert", "last": time.monotonic(), "tool": None,
                                         "text": text, "count": count, "on_click": on_click, "ttl": ttl_s}
        self.touch()
        self.jump = [2, 4, 4, 2]

    def get_alert(self, key: str) -> dict | None:
        return self.sessions.get(f"alert:{key}")

    def clear_alert(self, key: str) -> None:
        self.sessions.pop(f"alert:{key}", None)

    def run_process(self, program: str, args: list, done, timeout_s: int = 60) -> None:
        """Run a program without blocking the UI. done(code, stdout, stderr); on timeout the code is 124."""
        proc = QProcess(self)
        timer = QTimer(self)
        timer.setSingleShot(True)
        state = {"finished": False, "timeout": False}
        self.processes.add(proc)

        def finish(code: int, extra: str = "") -> None:
            if state["finished"]:
                return
            state["finished"] = True
            timer.stop()
            out = bytes(proc.readAllStandardOutput()).decode(errors="replace").strip()
            err = bytes(proc.readAllStandardError()).decode(errors="replace").strip()
            if state["timeout"]:
                code, err = 124, _("Timed out ({seconds}s). {detail}", seconds=timeout_s, detail=err).strip()
            self.processes.discard(proc)
            # Disconnect the Python slots before deleting: with PySide6 6.12 on Linux, a QProcess deleted while its
            # lambdas are still connected crashes the next event loop pass (SIGSEGV/SIGBUS), long after this call.
            for signal in (proc.finished, proc.errorOccurred):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            proc.deleteLater()
            timer.deleteLater()
            done(code, out, err or extra)

        def on_timeout() -> None:
            state["timeout"] = True
            proc.kill()

        proc.finished.connect(lambda code, _status: finish(code))
        proc.errorOccurred.connect(lambda _e: finish(1, _("Could not run {program}.", program=program))
                                   if proc.state() == QProcess.NotRunning else None)
        timer.timeout.connect(on_timeout)
        proc.start(program, [str(a) for a in args])
        timer.start(timeout_s * 1000)

    def show_text(self, title: str, text: str, ok: bool = True) -> None:
        box = QMessageBox(QMessageBox.Information if ok else QMessageBox.Warning, title,
                          (text or _("(no output)"))[:3000], QMessageBox.Ok, self)
        box.setTextFormat(Qt.PlainText)
        box.exec()

    # ---- agents panel and managed agents ------------------------------------
    def record_agent_event(self, data: dict) -> None:
        if self.agents is None:
            return
        try:
            self.agents.record(data)
            self.agents_active = self.agents.active_count()
            if self.agents_panel is not None and self.agents_panel.isVisible():
                self.agents_panel.refresh()
                self.agents_panel.on_hook_event(data)
        except Exception as e:
            print(f"NINA: could not record an agent event: {e}", file=sys.stderr)

    def prune_sessions(self) -> None:
        """Conversations NINA keeps for agents on other AIs: drop those nobody refers to any more (no open chat, no
        history entry, no managed agent) after `history_keep_days` days, or 90 when the history is kept forever."""
        from nina.agents import managed_store, sessions
        days = self.cfg.get("history_keep_days", 0)
        days = days if isinstance(days, int) and days > 0 else 90
        keep = {m.get("session") for m in chat.chathistory.list_meta()} if chat is not None else set()
        keep |= {a.get("session") for a in managed_store.load()}
        if chat is not None:
            for t in chat.load_tabs().get("tabs", []):
                keep.add(chat.load_saved(chat.tab_state_path(t["id"])).get("session"))
        sessions.prune(days, {k for k in keep if k})

    def open_connections(self) -> None:
        """The AIs NINA can use and which one each role runs on."""
        from nina.agents import roles as roles_mod
        from nina.connections_view import ConnectionsDialog
        dialog = ConnectionsDialog(self.cfg, config.list_accounts(), self.save_connections,
                                   roles=roles_mod.load_roles(config.roles_dir()))
        dialog.exec()

    def save_connections(self, new: dict) -> None:
        for key in ("connections", "default_connection", "role_connections"):
            self.cfg[key] = new.get(key, self.cfg.get(key))
        config.save_config(self.cfg)
        if self.chat_box is not None:
            self.chat_box.update_account_button()

    def managed_available(self) -> bool:
        """Agents and the chat need either the Claude Agent SDK or a connection to another AI."""
        if managed_view is None or managed is None:
            return False
        return managed.available() or bool(connections.custom_connections(self.cfg))

    def managed_context(self) -> dict:
        return {"accounts": config.list_accounts(), "account": self.cfg.get("account", ""),
                "accounts_root": config.ACCOUNTS_ROOT, "default_dir": config.default_dir_of(self.cfg),
                "roles_dir": config.roles_dir(), "claude_bin": self.cfg.get("claude_bin", ""),
                "cfg": self.cfg, "sdk": managed is not None and managed.available()}

    def make_managed_tab(self):
        if not self.managed_available():
            return None
        try:
            tab = managed_view.ManagedTab(self.managed_context)
        except Exception as e:   # without managed agents the panel keeps working
            print(f"NINA: managed agents unavailable: {e}", file=sys.stderr)
            return None
        tab.attention.connect(lambda text: self.say(text, 4))
        tab.hook_event.connect(self.record_agent_event)
        QApplication.instance().aboutToQuit.connect(tab.shutdown)
        return tab

    # ---- talking to NINA ----------------------------------------------------
    def ensure_chat(self) -> bool:
        if self.chat_box is not None:
            return True
        if not self.managed_available():
            QMessageBox.information(self, APP_NAME, _("Talking to NINA needs managed agents (the 'agents' extra) or a "
                                                      "connection to another AI (right click → Connections and agents...)."))
            return False
        saved = chat.load_tabs()
        days = self.cfg.get("history_keep_days", 0)
        if isinstance(days, int) and days > 0:        # optional: forget conversations unused for this many days
            chat.chathistory.prune(days, keep={t.get("conv") for t in saved.get("tabs", [])})
        self.prune_sessions()
        first = saved["tabs"][0]["id"] if saved else "main"
        self.chat_box = chat.ChatBox(self.make_chat_controller(first), self.cfg.get("chat_theme", "dark"),
                                     self.save_chat_theme, factory=self.make_chat_controller, saved=saved,
                                     on_connections=self.open_connections)
        return True

    @property
    def chat_controller(self):
        """The conversation on screen (each open chat has its own)."""
        return self.chat_box.controller if self.chat_box is not None else None

    def make_chat_controller(self, tab_id: str):
        ctrl = chat.ChatController(lambda: {**self.managed_context(), "chat_role": self.cfg.get("chat_role", "")},
                                   path=chat.tab_state_path(tab_id), parent=self, tab_id=tab_id, auto=self.auto_mode())
        ctrl.attention.connect(lambda text: self.say(text, 4))
        ctrl.hook_event.connect(self.record_agent_event)
        ctrl.state_changed.connect(lambda state, c=ctrl: self.on_chat_state(c, state))
        ctrl.message.connect(lambda *_a, c=ctrl: self.on_chat_activity(c))
        ctrl.finished.connect(self.celebrate)
        ctrl.permission_asked.connect(lambda req, c=ctrl: self.on_chat_permission(c, req))
        ctrl.permission_cleared.connect(lambda c=ctrl: self.on_chat_state(c, c.status))
        return ctrl

    def auto_mode(self):
        """The auto mode switch and saved rules, shared by every chat."""
        if getattr(self, "_auto_mode", None) is None:
            self._auto_mode = autoapprove.AutoMode(bool(self.cfg.get("chat_auto", False)), autoapprove.Rules(),
                                                   self.save_chat_auto)
        return self._auto_mode

    def save_chat_auto(self, enabled: bool) -> None:
        self.cfg["chat_auto"] = enabled
        config.save_config(self.cfg)

    def save_chat_theme(self, theme: str) -> None:
        self.cfg["chat_theme"] = theme
        config.save_config(self.cfg)

    @staticmethod
    def chat_key(ctrl) -> str:
        return f"nina-chat-{ctrl.tab_id}"

    def on_chat_permission(self, ctrl, _req: dict) -> None:
        self.sessions[self.chat_key(ctrl)] = {"state": "waiting", "last": time.monotonic(), "tool": None,
                                              "text": _("I need your permission!")}
        self.jump = [2, 4, 4, 2]
        self.touch()
        self.update()

    def on_chat_state(self, ctrl, state: str) -> None:
        """Mirror the conversations in the mascot: working while one thinks, waiting while one needs you."""
        if ctrl.current_req is not None:
            return          # the permission alert stays until you answer
        if state in ("working", "starting"):
            self.sessions[self.chat_key(ctrl)] = {"state": "working", "last": time.monotonic(), "tool": None}
        else:
            self.sessions.pop(self.chat_key(ctrl), None)
        self.touch()
        self.update()

    def on_chat_activity(self, ctrl) -> None:
        st = self.sessions.get(self.chat_key(ctrl))
        if st is not None:
            st["last"] = time.monotonic()

    def toggle_chat(self, text: str = "") -> None:
        if not self.ensure_chat():
            return
        self.touch()
        if self.chat_box.isVisible() and not text:
            self.chat_box.hide()
            return
        self.chat_box.show_near(self)
        if text:
            self.chat_controller.ask(text)

    def new_agent(self) -> None:
        self.show_agents()
        if self.agents_panel is not None and self.agents_panel.managed_tab is not None:
            self.agents_panel.show_managed()
            self.agents_panel.managed_tab.new_agent()

    def show_agents(self) -> None:
        if self.agents is None:
            QMessageBox.information(self, APP_NAME, _("The agents panel is not available."))
            return
        if self.agents_panel is None:
            from nina.agents.panel import AgentsPanel
            self.agents_panel = AgentsPanel(self.agents, self.make_managed_tab(), on_resume=self.resume_in_terminal)
        self.agents_panel.show()
        self.agents_panel.raise_()
        self.agents_panel.activateWindow()

    # ---- external commands and Claude Code hooks ---------------------------
    def handle_hook(self, data: dict) -> None:
        self.record_agent_event(data)
        ev, sid = data.get("ev"), str(data.get("sid") or "?")
        now = time.monotonic()
        st = self.sessions.get(sid)
        if ev in ("SessionStart", "SubagentStart", "SubagentStop"):
            return   # these only feed the agents panel
        if ev == "SessionEnd":
            self.sessions.pop(sid, None)
            return
        if ev == "Stop":
            self.sessions.pop(sid, None)
            self.celebrate()
            self.say(_("Done!"), 2.5)
            return
        self.touch()
        if ev == "UserPromptSubmit":
            self.sessions[sid] = {"state": "working", "last": now, "tool": None}
        elif ev in ("PreToolUse", "PostToolUse"):
            if st is None:
                st = self.sessions[sid] = {"state": "working", "last": now, "tool": None}
            st["state"], st["last"] = "working", now
            label = TOOL_LABELS.get(str(data.get("tool") or ""))
            if ev == "PreToolUse" and label and label != st["tool"]:
                self.say(_(label), 2)
            if ev == "PreToolUse" and label:
                st["tool"] = label
        elif ev == "Notification":
            if data.get("ntype") in ("auth_success", "elicitation_complete", "elicitation_response"):
                return
            permission = (data.get("ntype") == "permission_prompt"
                          or "permission" in str(data.get("msg", "")).lower())
            self.sessions[sid] = {"state": "waiting", "last": now, "tool": st["tool"] if st else None,
                                  "text": _("I need your permission!") if permission else _("I'm waiting for you!")}
            self.jump = [2, 4, 4, 2]
            self.update()

    def handle_command(self, line: str) -> None:
        cmd, _sep, arg = line.strip().partition(" ")
        if cmd == "hook":
            try:
                data = json.loads(arg)
            except ValueError:
                return
            if isinstance(data, dict):
                self.handle_hook(data)
        elif cmd == "show":
            self.touch()
            self.show()
            self.raise_()
            self.say(_("I'm here!"))
        elif cmd == "chat":
            self.toggle_chat()
        elif cmd == "ask" and arg:
            self.toggle_chat(arg)
        elif cmd == "say" and arg:
            self.touch()
            self.say(arg, 4)
        elif cmd == "happy":
            self.celebrate()
            if arg:
                self.say(arg, 3)
        elif cmd == "sleep":
            self.sleeping = True
