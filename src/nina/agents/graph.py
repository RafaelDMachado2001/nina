"""
Agent graph model and force-directed layout. Pure (no Qt): testable in isolation.

Nodes: NINA (center), sessions, subagents, tools (and MCP servers) and the files they touched.
Edges: session<->NINA, session->subagent (spawned), agent->tool (called) and agent->file (read/wrote).
The graph is computed from the agents and events up to an instant `cutoff`: the same code serves live
(cutoff = now) and replay (cutoff = a point on the timeline).
"""
import math
import zlib

from nina.i18n import _

HUB = "nina"
STALE_S = 180
FILE_READ = {"Read"}
FILE_WRITE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_FILES_PER_AGENT = 8
MAX_AGENTS = 24
REPULSION = 1800.0      # force between nodes (~1/d), with a limited range: keeps the graph from "exploding"
REPULSION_RANGE = 230.0
SPRING = 0.09
GRAVITY = 0.045
LINK_LENGTH = {"session": 150, "spawn": 90, "tool": 120, "mcp": 120, "read": 75, "write": 75}


def tool_node(tool: str) -> tuple:
    """(id, label, kind) of a tool node; MCP servers become one node per server."""
    if tool.startswith("mcp__"):
        server = tool.split("__")[1] if tool.count("__") >= 2 else tool[5:]
        return f"mcp:{server}", server, "mcp"
    return f"tool:{tool}", tool, "tool"


def file_node(path: str) -> tuple:
    return f"file:{path}", (path.rstrip("/").rsplit("/", 1)[-1] or path), "file"


def _status(events: list, cutoff: float) -> str:
    if not events:
        return "idle"
    last = events[-1]
    kind = last["type"]
    if kind in ("done", "end", "waiting"):
        return {"done": "done", "end": "ended", "waiting": "waiting"}[kind]
    return "working" if cutoff - last["ts"] <= STALE_S else "stale"


def build_graph(agents: list, events: list, cutoff: float) -> dict:
    """agents: rows from `AgentStore.all_agents()`; events: `AgentStore.all_events()` (chronological order)."""
    visible = [a for a in agents if a["started_at"] <= cutoff]
    visible.sort(key=lambda a: a["last_event_at"], reverse=True)
    sessions = [a for a in visible if a["kind"] == "session"][:MAX_AGENTS]
    keep = {a["id"] for a in sessions}
    chosen = sessions + [a for a in visible if a["kind"] == "subagent" and a["parent_id"] in keep]
    by_id = {a["id"]: a for a in chosen}

    per_agent = {}
    for e in events:
        if e["ts"] <= cutoff and e["agent_id"] in by_id:
            per_agent.setdefault(e["agent_id"], []).append(e)

    nodes = {HUB: {"label": "NINA", "kind": "hub", "status": "hub", "count": 0}}
    edges = {}

    def edge(a: str, b: str, kind: str, ts: float, inc: int = 1) -> None:
        cur = edges.setdefault((a, b), {"kind": kind, "count": 0, "ts": ts})
        cur["count"] += inc
        cur["ts"] = max(cur["ts"], ts)

    for a in chosen:
        evs = per_agent.get(a["id"], [])
        title = a["title"] or (a["agent_type"] if a["kind"] == "subagent" else _("session {id}", id=a["id"][:6]))
        nodes[a["id"]] = {"label": str(title or _("agent"))[:30], "kind": a["kind"],
                          "status": _status(evs, cutoff), "count": len(evs), "type": a["agent_type"]}
        if a["kind"] == "session":
            edge(HUB, a["id"], "session", a["started_at"])
        else:
            edge(a["parent_id"], a["id"], "spawn", a["started_at"])
        recent_files = {}
        for e in evs:
            if e["type"] != "tool" or not e["tool"]:
                continue
            nid, label, kind = tool_node(e["tool"])
            node = nodes.setdefault(nid, {"label": label, "kind": kind, "status": "tool", "count": 0})
            node["count"] += 1
            edge(a["id"], nid, kind, e["ts"])
            if e["summary"] and (e["tool"] in FILE_READ or e["tool"] in FILE_WRITE):
                mode = "read" if e["tool"] in FILE_READ else "write"
                recent_files[e["summary"]] = (mode, e["ts"])
        newest = sorted(recent_files.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_FILES_PER_AGENT]
        for path, (mode, ts) in newest:
            nid, label, kind = file_node(path)
            node = nodes.setdefault(nid, {"label": label, "kind": "file", "status": mode, "count": 0, "path": path})
            node["count"] += 1
            if mode == "write":
                node["status"] = "write"
            edge(a["id"], nid, mode, ts)
    return {"nodes": nodes, "edges": edges, "agents": by_id}


def pulses_for(agent_id: str, etype: str, tool: str | None, summary: str | None, parent_id: str | None) -> list:
    """Edges an event travels along (to animate the data flow): [(source, target)]."""
    if etype == "prompt":
        return [(HUB, agent_id)]
    if etype == "start" and parent_id:
        return [(parent_id, agent_id)]
    if etype == "tool" and tool:
        out = [(agent_id, tool_node(tool)[0])]
        if summary and (tool in FILE_READ or tool in FILE_WRITE):
            out.append((agent_id, file_node(summary)[0]))
        return out
    if etype == "done":
        return [(agent_id, parent_id or HUB)]
    if etype == "waiting":
        return [(agent_id, HUB)]
    return []


class ForceLayout:
    """Incremental force layout: new nodes are born near a neighbor and the graph settles gradually."""

    def __init__(self):
        self.pos = {}
        self.vel = {}
        self.pinned = set()

    @staticmethod
    def _seed(nid: str, radius: float) -> list:
        h = zlib.crc32(nid.encode())
        angle = (h % 3600) / 3600 * 2 * math.pi
        return [radius * math.cos(angle), radius * math.sin(angle)]

    def update(self, nodes: dict, edges: dict) -> bool:
        """Sync the nodes. Returns True if anything changed (to reheat the simulation)."""
        changed = False
        for nid in [n for n in self.pos if n not in nodes]:
            del self.pos[nid], self.vel[nid]
            self.pinned.discard(nid)
            changed = True
        neighbors = {}
        for a, b in edges:
            neighbors.setdefault(a, []).append(b)
            neighbors.setdefault(b, []).append(a)
        for nid in nodes:
            if nid in self.pos:
                continue
            anchor = next((n for n in neighbors.get(nid, []) if n in self.pos), None)
            if nid == HUB:
                self.pos[nid] = [0.0, 0.0]
            elif anchor is not None:
                ax, ay = self.pos[anchor]
                dx, dy = self._seed(nid, 40)
                self.pos[nid] = [ax + dx, ay + dy]
            else:
                self.pos[nid] = self._seed(nid, 160)
            self.vel[nid] = [0.0, 0.0]
            changed = True
        return changed

    def step(self, edges: dict, temp: float = 1.0) -> float:
        """One iteration. Returns the energy (sum of speeds) so the caller can tell when it has settled."""
        ids = list(self.pos)
        force = {n: [0.0, 0.0] for n in ids}
        for i, a in enumerate(ids):
            ax, ay = self.pos[a]
            for b in ids[i + 1:]:
                dx, dy = ax - self.pos[b][0], ay - self.pos[b][1]
                d2 = dx * dx + dy * dy
                if d2 < 0.25:
                    dx, dy, d2 = 0.7, 0.4, 0.65
                if d2 > REPULSION_RANGE * REPULSION_RANGE:
                    continue
                push = REPULSION / d2       # repulsion ~ 1/d
                force[a][0] += dx * push
                force[a][1] += dy * push
                force[b][0] -= dx * push
                force[b][1] -= dy * push
        for (a, b), e in edges.items():
            if a not in self.pos or b not in self.pos:
                continue
            dx, dy = self.pos[b][0] - self.pos[a][0], self.pos[b][1] - self.pos[a][1]
            d = math.hypot(dx, dy) or 0.01
            pull = SPRING * (d - LINK_LENGTH.get(e["kind"], 100)) / d
            force[a][0] += dx * pull
            force[a][1] += dy * pull
            force[b][0] -= dx * pull
            force[b][1] -= dy * pull
        energy = 0.0
        for n in ids:
            if n == HUB or n in self.pinned:
                self.pos[n] = [0.0, 0.0] if n == HUB else self.pos[n]
                self.vel[n] = [0.0, 0.0]
                continue
            fx = force[n][0] - GRAVITY * self.pos[n][0]     # gravity toward the center
            fy = force[n][1] - GRAVITY * self.pos[n][1]
            vx = (self.vel[n][0] + fx) * 0.82
            vy = (self.vel[n][1] + fy) * 0.82
            speed = math.hypot(vx, vy)
            cap = 14.0 * temp
            if speed > cap:
                vx, vy = vx * cap / speed, vy * cap / speed
            self.vel[n] = [vx, vy]
            self.pos[n][0] += vx
            self.pos[n][1] += vy
            energy += abs(vx) + abs(vy)
        return energy

    def settle(self, edges: dict, iterations: int = 200) -> None:
        for _i in range(iterations):
            if self.step(edges) < 0.02 * max(1, len(self.pos)):
                break
