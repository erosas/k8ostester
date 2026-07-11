"""Live run view: a rich renderable fed by runner events.

`k8ost run` hands `on_event` to the Runner and the whole object to rich's
Live. The panel shows the experiment identity and current step, load-phase
progress, a metrics column (ops/s, errors, live goal scores from `load.sample`
events), the cluster topology (`topology` events — primary flips are visible
during failover), and a rolling tail of the event log.
"""

from __future__ import annotations

import time
from collections import deque

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

ALERT_TYPES = ("run.error", "verify.fail", "goal.fail", "teardown.error", "capability.warn")
# folded into the metrics/topology panes instead of the event tail
PANE_TYPES = ("load.sample", "topology")

# shape says role, color says status: green = healthy/streaming,
# yellow = transitioning, red = broken, plain = no health information
NODE_ICONS = {"client": "▷", "proxy": "◆", "primary": "●", "replica": "○"}
NODE_STATUS_STYLES = {
    "healthy": "green", "replicating": "yellow", "failed": "bold red",
}
REPLICATION_STREAMING = {"sync", "async", "quorum"}
EDGE_STATUS_STYLES = {"detached": "bold red", "potential": "yellow"}


def _node_style(node: dict) -> str:
    return NODE_STATUS_STYLES.get(node.get("detail", ""), "")


def _edge_style(label: str) -> str:
    if label in REPLICATION_STREAMING:
        return "green"
    return EDGE_STATUS_STYLES.get(label, "dim")


def topology_text(data: dict) -> Text:
    """Render a topology event as a connection tree — how traffic and
    replication flow (`loadgen ─▶ pooler ─▶ primary ─sync─▶ replica`).
    Accepts the graph form ({nodes, edges}) from `topology_graph()`; falls
    back to flat primary/replicas bullets for drivers that only have that."""
    nodes = data.get("nodes")
    if not nodes:
        lines = []
        if primary := data.get("primary"):
            lines.append(Text.assemble(("● ", "bold green"), (primary, "bold"), ("  primary", "dim")))
        for replica in data.get("replicas", []):
            lines.append(Text.assemble(("○ ", "cyan"), replica, ("  replica", "dim")))
        return Text("\n").join(lines)

    by_id = {n["id"]: n for n in nodes}
    children: dict[str, list[dict]] = {}
    has_parent = set()
    for edge in data.get("edges", []):
        children.setdefault(edge["source"], []).append(edge)
        has_parent.add(edge["target"])

    lines: list[Text] = []
    seen: set[str] = set()

    def emit(node_id: str, prefix: str, via: dict | None, last: bool) -> None:
        node = by_id.get(node_id, {"id": node_id, "role": ""})
        role = node.get("role", "")
        line = Text(prefix)
        if via is not None:
            label = via.get("detail") or ""
            line.append("└─" if last else "├─", style="dim")
            if label:
                line.append(label, style=_edge_style(label))
            line.append("─▶ ", style="dim")
        line.append(NODE_ICONS.get(role, "•") + " ", style=_node_style(node))
        line.append(node_id, style="bold" if role == "primary" else "")
        if role:
            line.append(f"  {role}", style="dim")
        if detail := node.get("detail"):
            line.append(f"  {detail}", style=_node_style(node) or "dim")
        # a node reachable via several paths (e.g. a bypassed pooler also
        # points at the primary) renders its subtree only once
        if node_id in seen:
            line.append("  ↩", style="dim")
            lines.append(line)
            return
        seen.add(node_id)
        lines.append(line)
        child_prefix = prefix if via is None else prefix + ("   " if last else "│  ")
        outgoing = children.get(node_id, [])
        for i, child in enumerate(outgoing):
            emit(child["target"], child_prefix, child, i == len(outgoing) - 1)

    roots = [n["id"] for n in nodes if n["id"] not in has_parent]
    for root in roots:
        emit(root, "", None, True)
    return Text("\n").join(lines)


class LiveRunView:
    def __init__(self, name: str, technology: str, context: str | None, tail: int = 10):
        self.name = name
        self.technology = technology
        self.context = context or "(current)"
        self.events: deque[dict] = deque(maxlen=tail)
        self.started = time.time()
        self.sample: dict | None = None
        self.topology: dict | None = None
        self.load_total_s: float | None = None
        self.load_started_at: float | None = None
        self._spinner = Spinner("dots", style="cyan")

    def on_event(self, event: dict) -> None:
        etype = event["type"]
        data = event.get("data", {})
        if etype == "load.sample":
            self.sample = data
            return
        if etype == "topology":
            self.topology = data
            return
        if etype == "load.start":
            self.load_total_s = data.get("total_s")
            self.load_started_at = time.time()
        self.events.append(event)

    # -- render pieces ---------------------------------------------------------

    def _header(self) -> RenderableType:
        current = self.events[-1]["type"] if self.events else "starting"
        grid = Table.grid(padding=(0, 2))
        grid.add_row(
            self._spinner,
            Text(f"{self.name} ({self.technology})", style="bold"),
            Text(f"context {self.context}", style="dim"),
            Text(f"{time.time() - self.started:6.1f}s", style="dim"),
            Text(current, style="cyan"),
        )
        return grid

    def _progress(self) -> RenderableType | None:
        if not (self.load_total_s and self.load_started_at):
            return None
        done = min(time.time() - self.load_started_at, self.load_total_s)
        grid = Table.grid(padding=(0, 1))
        grid.add_row(
            Text("load", style="dim"),
            ProgressBar(total=self.load_total_s, completed=done, width=40),
            Text(f"{done:.0f}/{self.load_total_s:.0f}s", style="dim"),
        )
        return grid

    def _metrics(self) -> RenderableType | None:
        if not self.sample:
            return None
        s = self.sample
        rates = Text.assemble(
            (f"{s['ops_s']:.1f}", "bold green"), (" ops/s   ", "dim"),
            (f"{s['err_s']:.1f}", "bold red" if s["err_s"] else "bold"), (" err/s", "dim"),
        )
        totals = Text(
            f"{s['total_ops']} ops · {s['acked_writes']} acked writes · {s['failed']} failed",
            style="dim",
        )
        goals = Table.grid(padding=(0, 1))
        for g in s.get("goals", []):
            goals.add_row(
                Text("✔", style="green") if g["passed"] else Text("✘", style="red"),
                Text(g["goal"]),
                Text(str(g["value"]), style="bold"),
                Text(f"({g['threshold']})", style="dim"),
            )
        return Panel(Group(rates, totals, goals), title="metrics",
                     title_align="left", border_style="dim", expand=False)

    def _topology_pane(self) -> RenderableType | None:
        if not self.topology:
            return None
        return Panel(topology_text(self.topology), title="topology",
                     title_align="left", border_style="dim", expand=False)

    def _tail(self) -> RenderableType:
        grid = Table.grid(padding=(0, 1))
        for e in self.events:
            alert = e["type"] in ALERT_TYPES
            grid.add_row(
                Text(f"{e['t_rel']:>8.1f}s", style="dim"),
                Text(f"{e['type']:<18}", style="bold red" if alert else "bold"),
                Text(e["msg"], style="red" if alert else ""),
            )
        return grid

    def __rich__(self) -> RenderableType:
        parts: list[RenderableType] = [self._header()]
        if progress := self._progress():
            parts.append(progress)
        panes = [p for p in (self._metrics(), self._topology_pane()) if p]
        if panes:
            parts.append(Columns(panes, padding=(0, 1)))
        parts.append(self._tail())
        return Panel(Group(*parts), title="k8ost run", title_align="left")