"""Live run view: a rich renderable fed by runner events.

`k8ost run` hands `on_event` to the Runner and the whole object to rich's
Live — the header shows the experiment identity, elapsed time and the current
step; below it a rolling tail of the run's event log.
"""

from __future__ import annotations

import time
from collections import deque

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

ALERT_TYPES = ("run.error", "verify.fail", "goal.fail", "teardown.error", "capability.warn")


class LiveRunView:
    def __init__(self, name: str, technology: str, context: str | None, tail: int = 14):
        self.name = name
        self.technology = technology
        self.context = context or "(current)"
        self.events: deque[dict] = deque(maxlen=tail)
        self.started = time.time()
        self._spinner = Spinner("dots", style="cyan")

    def on_event(self, event: dict) -> None:
        self.events.append(event)

    def __rich__(self) -> RenderableType:
        elapsed = time.time() - self.started
        current = self.events[-1]["type"] if self.events else "starting"
        header = Table.grid(padding=(0, 2))
        header.add_row(
            self._spinner,
            Text(f"{self.name} ({self.technology})", style="bold"),
            Text(f"context {self.context}", style="dim"),
            Text(f"{elapsed:6.1f}s", style="dim"),
            Text(current, style="cyan"),
        )
        events = Table.grid(padding=(0, 1))
        for e in self.events:
            alert = e["type"] in ALERT_TYPES
            events.add_row(
                Text(f"{e['t_rel']:>8.1f}s", style="dim"),
                Text(f"{e['type']:<18}", style="bold red" if alert else "bold"),
                Text(e["msg"], style="red" if alert else ""),
            )
        return Panel(Group(header, events), title="k8ost run", title_align="left")