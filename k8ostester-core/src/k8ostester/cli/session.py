"""`k8ost session` — the interactive lab.

Deploy an experiment's config, then drive it by hand: scale the load pool up
and down (each load pod is a self-contained unit of load, so the pod count is
the knob) and fire failure modes on demand, all while watching the same
dashboard as `k8ost run` — metrics, live goal scores, topology, events. The
laptop is only the driver: load runs in-cluster, so remote clusters behave
identically. Quit tears the namespace down unless --keep.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, ProgressBar, RichLog, Static

from k8ostester.cli.app import app, console
from k8ostester.cli.run import pick_experiment
from k8ostester.cli.tui import RunApp
from k8ostester.core.experiment import load_experiment

FAULT_BUTTONS = {
    # button id → (worker, target, duration)
    "kill-primary": ("pod_kill", {"role": "primary"}, None),
    "kill-replica": ("pod_kill", {"role": "replica"}, None),
    "partition-primary": ("network_partition", {"role": "primary"}, "30s"),
}


class SessionApp(RunApp):
    """The run dashboard plus a controls bar; the session loop replaces the
    runner on the worker thread."""

    TITLE = "k8ost session"
    BINDINGS = [
        ("q", "leave", "Quit + teardown"),
        ("plus", "scale(1)", "Load +1 pod"),
        ("minus", "scale(-1)", "Load -1 pod"),
        ("k", "fault('kill-primary')", "Kill primary"),
        ("r", "fault('kill-replica')", "Kill replica"),
        ("p", "fault('partition-primary')", "Partition 30s"),
    ]
    CSS = RunApp.CSS + """
    #controls { height: 5; margin: 0 1; border: round $surface-lighten-2; }
    #controls Button { margin: 0 1 0 0; min-width: 12; }
    #pods { width: 16; content-align: center middle; }
    """

    def __init__(self, spec, context, session):
        super().__init__(spec, context, make_runner=lambda cb: None)  # unused
        self.session = session
        self._ended_status: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield ProgressBar(id="load-progress", show_eta=False)
        with Horizontal(id="panes"):
            with Vertical(id="metrics-pane") as metrics:
                metrics.border_title = "metrics"
                yield Static(id="m-rates")
                yield DataTable(id="m-goals", cursor_type="row")
            with Vertical(id="topology-pane") as topology:
                topology.border_title = "topology"
                yield Static(id="t-current")
                yield RichLog(id="t-history", markup=False, highlight=False)
        with Horizontal(id="controls") as controls:
            controls.border_title = "controls"
            yield Button("load −", id="scale-down")
            yield Static(id="pods")
            yield Button("load +", id="scale-up", variant="success")
            yield Button("kill primary", id="kill-primary", variant="error")
            yield Button("kill replica", id="kill-replica", variant="warning")
            yield Button("partition 30s", id="partition-primary", variant="warning")
        events_log = RichLog(id="e-log", markup=False, highlight=False)
        events_log.border_title = "events"
        yield events_log
        yield Footer()

    def on_mount(self) -> None:
        # textual dispatches on_mount for every class in the MRO — RunApp's
        # setup has already run; only add the session-specific bits here
        self._show_pods(self.session.pods)

    def _seed_goal_rows(self) -> None:
        # sessions have no verdict: seed only the live-scorable goal rows
        table = self.query_one("#m-goals", DataTable)
        for g in self.spec.goals:
            if not g.metric:
                continue
            threshold = f"max {g.max}" if g.max is not None else f"min {g.min}"
            table.add_row(Text("·"), g.metric, Text("—", style="dim"),
                          threshold, "", key=g.metric)

    # -- worker: the session loop instead of a runner ---------------------------

    def _execute(self) -> None:
        try:
            self.session.start()
        except Exception as e:
            self.call_from_thread(self._session_ended, "error", str(e))
        else:
            self.call_from_thread(self._session_ended, "ended", None)

    def _session_ended(self, status: str, message: str | None) -> None:
        self._ended_status = status
        self.status = status
        if message:
            self.error = message
            self.query_one("#e-log", RichLog).write(
                Text(f"session error: {message}", style="bold red"))
        self.exit(1 if status == "error" else 0)

    # -- controls ---------------------------------------------------------------

    def action_scale(self, delta: int) -> None:
        self.session.scale(delta)

    def action_fault(self, button_id: str) -> None:
        worker, target, duration = FAULT_BUTTONS[button_id]
        self.session.inject(worker, target, duration)
        self.notify(f"{worker} on {target.get('role', '?')} requested", timeout=4)

    def action_leave(self) -> None:
        if self._ended_status is None:
            self.notify("stopping session — tearing down…", timeout=8)
            self.session.stop()  # worker finishes → _session_ended → exit
        else:
            self.exit(1 if self._ended_status == "error" else 0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "scale-up":
            self.session.scale(1)
        elif button_id == "scale-down":
            self.session.scale(-1)
        elif button_id in FAULT_BUTTONS:
            self.action_fault(button_id)

    # -- ingestion additions ------------------------------------------------------

    def _ingest(self, event: dict) -> None:
        super()._ingest(event)
        if event["type"] == "load.scale":
            self._show_pods(event.get("data", {}).get("pods", self.session.pods))

    def _show_pods(self, pods: int) -> None:
        self.query_one("#pods", Static).update(Text.assemble(
            (str(pods), "bold"), (" load pod(s)", "dim"),
            (f"\n≈{pods * self.session.rate:g} ops/s", "dim"),
        ))

    def _tick(self) -> None:
        running = self._ended_status is None
        elapsed = time.time() - self.started
        self.query_one("#header", Static).update(Text.assemble(
            (f"{self.spec.name} (session)", "bold"),
            (f"  context {self.context}", "dim"),
            (f"  {elapsed:6.1f}s  ", "dim"),
            (self.current_step if running else self.status, "cyan"),
        ))


@app.command()
def session(
    path: Path = typer.Argument(None, help="Experiment directory (omit to pick interactively)"),
    context: str = typer.Option(None, "--context", "-c", help="Override the experiment's kubeconfig context"),
    keep: bool = typer.Option(False, "--keep", help="Leave the namespace running after the session"),
    pods: int = typer.Option(1, "--pods", help="Initial load pods"),
    rate: float = typer.Option(20.0, "--rate", help="ops/s per load pod"),
    clients: int = typer.Option(5, "--clients", help="Clients per load pod"),
    allow_concurrent: bool = typer.Option(False, "--allow-concurrent",
                                          help="Run even if another experiment occupies the cluster"),
) -> None:
    """Interactive lab: deploy a config, scale load and fire faults by hand."""
    from k8ostester.core.session import Session

    if not console.is_terminal:
        console.print("[red]k8ost session needs a terminal[/red] — it is interactive by nature")
        raise typer.Exit(1)
    if path is None:
        path = pick_experiment()
    spec = load_experiment(path)

    tui: SessionApp | None = None

    def on_event(event: dict) -> None:
        if tui is not None:
            tui.call_from_thread(tui._ingest, event)

    live_session = Session(spec, keep=keep, context_override=context,
                           on_event=on_event, allow_concurrent=allow_concurrent,
                           pods=pods, rate=rate, clients=clients)
    tui = SessionApp(spec, context or spec.cluster.context, live_session)
    code = tui.run()
    raise typer.Exit(code if isinstance(code, int) else 1)