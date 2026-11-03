#!/usr/bin/env python3
"""
Regenerates docs/images/*: screenshots and GIFs rendered offscreen with FICTIONAL data.

Nothing real is read or written: HOME, config and state point to a throwaway temp directory, and every project,
session and file below is made up. Needs the `docs` extra (Pillow); the managed-agents shots also need the `agents` extra.

    pip install -e ".[agents,docs]"
    python scripts/make_screenshots.py            # writes docs/images/
    python scripts/make_screenshots.py /some/dir
"""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "images"
TMP = Path(tempfile.mkdtemp(prefix="nina-shots-"))
HOME = TMP / "home"
for sub in ("projects/acme-api", "projects/acme-web", "projects/acme-shop", ".claude/accounts/personal", ".claude/accounts/work"):
    (HOME / sub).mkdir(parents=True, exist_ok=True)
os.environ.update(QT_QPA_PLATFORM="offscreen", HOME=str(HOME), XDG_CONFIG_HOME=str(TMP / "config"),
                  XDG_STATE_HOME=str(TMP / "state"), XDG_RUNTIME_DIR=str(TMP), LANG="en_US.UTF-8")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

app = QApplication([])

from nina import claude_hooks, config, i18n  # noqa: E402
from nina.agents.panel import AgentsPanel  # noqa: E402
from nina.agents.store import AgentStore  # noqa: E402
from nina.widget import Nina  # noqa: E402

i18n.set_language("en")
BG = QColor("#1b1e24")
FONT = QFont("Sans", 10)


SHOWN_ROOT = "/home/dev/projects"   # fictional paths shown on screen (the real folders live in the temp home)


def log(msg: str) -> None:
    print(f"  {msg}")


def to_pil(pm: QPixmap) -> Image.Image:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pm.toImage().save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def save_gif(frames: list, path: Path, ms: int, colors: int = 96) -> None:
    flat = []
    for im in frames:
        bg = Image.new("RGBA", im.size, (27, 30, 36, 255))
        bg.alpha_composite(im)
        flat.append(bg.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
    flat[0].save(path, save_all=True, append_images=flat[1:], duration=ms, loop=0, optimize=True, disposal=2)
    log(f"{path.name}: {len(frames)} frames, {path.stat().st_size // 1024} KB")


def pump(n: int = 3, pause: float = 0.0) -> None:
    for _ in range(n):
        app.processEvents()
        if pause:
            time.sleep(pause)


# ---- fictional data --------------------------------------------------------------------------
def demo_events(store: AgentStore, now: float) -> None:
    def ev(name, dt, sid="api", **kw):
        store.record({"ev": name, "sid": sid, **kw}, now=now + dt)

    api, web, shop, old = (f"{SHOWN_ROOT}/{p}" for p in ("acme-api", "acme-web", "acme-shop", "acme-billing"))
    ev("UserPromptSubmit", -840, prompt="Add rate limiting to the API", cwd=api)
    for dt, tool, detail in ((-820, "Grep", "rate limit"), (-800, "Read", "src/api/middleware.py")):
        ev("PreToolUse", dt, tool=tool, detail=detail)
    ev("SubagentStart", -780, aid="a1", atype="Explore", cwd=api)
    ev("PreToolUse", -770, aid="a1", atype="Explore", tool="Grep", detail="Middleware")
    ev("PreToolUse", -760, aid="a1", atype="Explore", tool="Read", detail="src/api/routes.py")
    ev("SubagentStop", -700, aid="a1", atype="Explore", last="Found 3 middleware entry points")
    for dt, tool, detail in ((-650, "mcp__tracker__get_issue", "ACME-142"), (-600, "Write", "src/api/rate_limiter.py"),
                             (-560, "Edit", "src/api/middleware.py"), (-520, "Write", "tests/test_rate_limiter.py"),
                             (-30, "Bash", "pytest tests/test_rate_limiter.py -q")):
        ev("PreToolUse", dt, tool=tool, detail=detail)

    ev("UserPromptSubmit", -500, sid="web", prompt="Write the v2.0 release notes", cwd=web)
    for dt, tool, detail in ((-480, "Read", "CHANGELOG.md"), (-450, "mcp__docs__search", "release process"),
                             (-420, "Read", "docs/RELEASE_NOTES.md"), (-400, "WebSearch", "semantic versioning")):
        ev("PreToolUse", dt, sid="web", tool=tool, detail=detail)
    ev("Notification", -45, sid="web", msg="Claude needs your permission to use Edit")

    ev("UserPromptSubmit", -3000, sid="shop", prompt="Fix the flaky checkout test", cwd=shop)
    for dt, tool, detail in ((-2960, "Bash", "pytest tests/test_checkout.py -x"), (-2900, "Read", "tests/test_checkout.py"),
                             (-2860, "Read", "src/api/middleware.py"), (-2800, "Edit", "tests/test_checkout.py")):
        ev("PreToolUse", dt, sid="shop", tool=tool, detail=detail)
    ev("Stop", -2600, sid="shop", last="Fixed: the test relied on wall-clock time.")

    ev("UserPromptSubmit", -9000, sid="old", prompt="Explore the billing module", cwd=old)
    ev("SessionEnd", -8000, sid="old")


# ---- the mascot -------------------------------------------------------------------------------
def write_config() -> None:
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({
        "account": "personal", "default_dir": str(HOME / "projects" / "acme-api"), "scale": 5,
        "recent": [str(HOME / "projects" / "acme-web"), str(HOME / "projects" / "acme-shop")]}))


def mascot_cell(nina: Nina, caption: str, size=(260, 230)) -> QPixmap:
    cell = QPixmap(*size)
    cell.fill(BG)
    p = QPainter(cell)
    shot = nina.grab()
    p.drawPixmap((size[0] - shot.width()) // 2, 8, shot)
    p.setPen(QColor("#c4c9d2"))
    p.setFont(FONT)
    p.drawText(0, size[1] - 34, size[0], 26, Qt.AlignCenter, caption)
    p.end()
    return cell


def reset(nina: Nina) -> None:
    nina.sessions.clear()
    nina.sleeping = False
    nina.happy_ticks = nina.bubble_ticks = 0
    nina.jump = []
    nina.bubble_text = ""
    nina.blink_ticks = 0


def mascot_states(nina: Nina) -> None:
    cells = []

    reset(nina)
    cells.append(mascot_cell(nina, "idle"))

    reset(nina)
    nina.t = 4
    nina.handle_hook({"ev": "UserPromptSubmit", "sid": "a"})
    nina.handle_hook({"ev": "PreToolUse", "sid": "a", "tool": "Edit"})
    nina.bubble_ticks = 999
    cells.append(mascot_cell(nina, "working"))

    reset(nina)
    nina.handle_hook({"ev": "Notification", "sid": "a", "ntype": "permission_prompt"})
    cells.append(mascot_cell(nina, "needs your permission"))

    reset(nina)
    nina.handle_hook({"ev": "Stop", "sid": "a"})
    nina.bubble_ticks = 999
    cells.append(mascot_cell(nina, "done"))

    reset(nina)
    nina.sleeping = True
    nina.t = 9
    cells.append(mascot_cell(nina, "asleep"))

    w, h = cells[0].width(), cells[0].height()
    sheet = QPixmap(w * len(cells), h)
    sheet.fill(BG)
    p = QPainter(sheet)
    for i, c in enumerate(cells):
        p.drawPixmap(i * w, 0, c)
    p.end()
    sheet.save(str(OUT / "mascot-states.png"))
    log("mascot-states.png")
    reset(nina)


def mascot_gif(nina: Nina) -> None:
    """A short story: idle -> working (tools change) -> waiting for permission -> back to working -> done."""
    script = {
        2: ("UserPromptSubmit", {}, "you send a prompt"),
        8: ("PreToolUse", {"tool": "Read"}, "Claude reads"),
        16: ("PreToolUse", {"tool": "Edit"}, "Claude edits"),
        24: ("PreToolUse", {"tool": "Bash"}, "Claude runs a command"),
        32: ("Notification", {"ntype": "permission_prompt"}, "Claude asks permission"),
        48: ("PostToolUse", {"tool": "Bash"}, "you approve: back to work"),
        56: ("Stop", {}, "Claude is done"),
    }
    reset(nina)
    frames, caption = [], "idle"
    for i in range(80):
        if i in script:
            name, extra, caption = script[i]
            nina.handle_hook({"ev": name, "sid": "demo", **extra})
        nina.tick()
        frames.append(to_pil(mascot_cell(nina, caption, size=(300, 240))))
    save_gif(frames, OUT / "mascot.gif", 120, colors=64)
    reset(nina)


def menu_shot(nina: Nina) -> None:
    claude_hooks.install_hooks()           # so "Connect to Claude Code" shows as checked
    nina.agents_active = 3                 # a realistic counter in "Agents (3 active)..."
    menu = nina.build_menu()
    menu.setStyleSheet("QMenu { font-size: 13px; }")
    menu.show()
    pump(3)
    menu.grab().save(str(OUT / "menu.png"))
    menu.close()
    log("menu.png")


# ---- panel and graph --------------------------------------------------------------------------
def panel_shots() -> None:
    now = time.time()
    store = AgentStore(":memory:")
    demo_events(store, now)
    panel = AgentsPanel(store)
    panel.resize(1180, 640)
    panel.show()
    pump(4, 0.02)
    first = panel.tree.topLevelItem(0)
    target = next((panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount())
                   if panel.tree.topLevelItem(i).childCount()), first)
    panel.tree.setCurrentItem(target)
    pump(3)
    panel.grab().save(str(OUT / "agents-panel.png"))
    log("agents-panel.png")

    panel.main_tabs.setCurrentIndex(1)
    gv = panel.graph_view
    pump(2)
    for _ in range(140):
        gv.tick()
    gv.pulse("api", "tool:Bash")
    gv.pulse("api", "file:src/api/rate_limiter.py")
    gv.pulse("web", "nina", "#ffd400")
    time.sleep(0.25)
    gv.tick()
    pump(2)
    panel.grab().save(str(OUT / "agent-graph.png"))
    log("agent-graph.png")

    # live graph GIF: new events arrive, nodes appear and pulses travel along the edges
    live = [
        ("PreToolUse", "api", {"tool": "Read", "detail": "src/api/limits.yaml"}),
        ("PreToolUse", "web", {"tool": "Edit", "detail": "docs/RELEASE_NOTES.md"}),
        ("PostToolUse", "web", {"tool": "Edit"}),
        ("SubagentStart", "api", {"aid": "a2", "atype": "code-reviewer"}),
        ("PreToolUse", "api", {"aid": "a2", "atype": "code-reviewer", "tool": "Read", "detail": "src/api/rate_limiter.py"}),
        ("PreToolUse", "api", {"aid": "a2", "atype": "code-reviewer", "tool": "Grep", "detail": "TODO"}),
        ("SubagentStop", "api", {"aid": "a2", "atype": "code-reviewer", "last": "2 small suggestions"}),
        ("PreToolUse", "api", {"tool": "Bash", "detail": "pytest -q"}),
        ("Stop", "api", {"last": "Rate limiting added and tested."}),
    ]
    frames = [to_pil(gv.grab().scaled(760, 440, Qt.KeepAspectRatio, Qt.SmoothTransformation))]
    t = now
    for name, sid, extra in live:
        data = {"ev": name, "sid": sid, **extra}
        store.record(data, now=t)
        t += 5
        panel.on_hook_event(data)
        for _ in range(11):
            gv.tick()
            time.sleep(0.075)
            frames.append(to_pil(gv.grab().scaled(760, 440, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
    save_gif(frames, OUT / "agent-graph.gif", 80)
    panel.close()


# ---- managed agents (needs the optional SDK) ----------------------------------------------------
def managed_shots() -> None:
    from nina.agents import managed, managed_view, roles
    if not managed.available():
        log("managed agents: claude-agent-sdk not installed, skipping those shots")
        return
    sdk = managed.sdk

    class FakeClient:
        """Stands in for the real SDK client so the shots need no login and spend nothing."""

        def __init__(self, options):
            self.options, self.queries = options, []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, text):
            self.queries.append(text)

        async def interrupt(self):
            pass

        async def receive_response(self):
            yield sdk.SystemMessage(subtype="init", data={"session_id": "8f3c2a91-demo"})
            yield sdk.AssistantMessage(content=[
                sdk.TextBlock(text="I looked at the project. It has no reviewer yet, so I will add a **pr-reviewer** that is read-only."),
                sdk.ToolUseBlock(id="t1", name="Glob", input={"pattern": ".claude/agents/*"})], model="demo")
            res = await self.options.can_use_tool("Write", {
                "file_path": ".claude/agents/pr-reviewer.md",
                "content": "---\nname: pr-reviewer\ndescription: Reviews pull requests for bugs, security and missing tests\n"
                           "tools: Read, Grep, Glob\nmodel: sonnet\n---\nYou review diffs. Cite file:line, order findings by severity,\n"
                           "and never modify files."}, None)
            ok = isinstance(res, sdk.PermissionResultAllow)
            yield sdk.AssistantMessage(content=[sdk.TextBlock(
                text="Created `.claude/agents/pr-reviewer.md`. Ask me to \"use the pr-reviewer\" on any branch." if ok else "OK, nothing was written.")],
                model="demo")
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                    session_id="8f3c2a91-demo", total_cost_usd=0.0)

    sdk.ClaudeSDKClient = FakeClient
    project = f"{SHOWN_ROOT}/acme-api"

    class StubDialog:
        def __init__(self, parent, rs, ctx):
            self.rs = rs

        def exec(self):
            return QDialog.Accepted

        def values(self):
            return {"role": self.rs["agent-builder"], "cwd": project, "account": "personal",
                    "prompt": "Create a read-only agent that reviews pull requests for this project"}

    real_dialog = managed_view.NewAgentDialog
    ctx = {"accounts": ["personal", "work"], "account": "personal", "accounts_root": config.ACCOUNTS_ROOT,
           "default_dir": project, "roles_dir": config.roles_dir(), "claude_bin": ""}

    # the real "New agent" dialog
    dialog = real_dialog(None, roles.load_roles(), ctx)
    dialog.role.setCurrentIndex(dialog.role.findData("agent-builder"))
    dialog.prompt.setPlainText("Create a read-only agent that reviews pull requests for this project")
    dialog.show()
    pump(3)
    dialog.grab().save(str(OUT / "new-agent-dialog.png"))
    dialog.close()
    log("new-agent-dialog.png")

    managed_view.NewAgentDialog = StubDialog
    panel = AgentsPanel(AgentStore(":memory:"), managed_view.ManagedTab(lambda: ctx))
    panel.resize(1180, 640)
    panel.show()
    panel.show_managed()
    shots = {"permission": False}

    def auto_click() -> None:
        box = QApplication.activeModalWidget()
        if box is None:
            return
        if not shots["permission"]:
            shots["permission"] = True
            box.grab().save(str(OUT / "permission-dialog.png"))
            log("permission-dialog.png")
        for b in box.buttons():
            if b.text() == "Allow":
                b.click()

    clicker = QTimer()
    clicker.timeout.connect(auto_click)
    clicker.start(60)
    tab = panel.managed_tab
    tab.new_agent()
    (agent_id,) = tab.agents
    t0 = time.time()
    while tab.agents[agent_id]["status"] != "idle" and time.time() - t0 < 10:
        pump(1, 0.01)
    pump(3)
    panel.grab().save(str(OUT / "managed-agents.png"))
    log("managed-agents.png")
    clicker.stop()
    tab.shutdown()
    panel.close()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Writing to {OUT} (temp home: {TMP})")
    write_config()
    nina = Nina()
    nina.show()
    pump(2)
    mascot_states(nina)
    mascot_gif(nina)
    menu_shot(nina)
    nina.timer.stop()
    nina.close()
    panel_shots()
    managed_shots()
    print("Done.")


if __name__ == "__main__":
    main()
