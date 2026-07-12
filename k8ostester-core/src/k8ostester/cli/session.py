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
from textual.widgets import Button, DataTable, Footer, ProgressBar, RichLog, Select, Static

from k8ostester.cli.app import app, console
from k8ostester.cli.run import pick_experiment
from k8ostester.cli.tui import RunApp
from k8ostester.core.experiment import load_experiment

AUTO_TARGETS = [("primary (auto)", "role:primary"), ("any replica (auto)", "role:replica")]
RATE_STEP = 5.0


class SessionApp(RunApp):
    """The run dashboard plus a controls bar; the session loop replaces the
    runner on the worker thread."""

    TITLE = "k8ost session"
    BINDINGS = [
        ("q", "leave", "Quit + teardown"),
        ("plus", "scale(1)", "Load +1 pod"),
        ("minus", "scale(-1)", "Load -1 pod"),
        ("right_square_bracket", "rate(1)", "Rate +"),
        ("left_square_bracket", "rate(-1)", "Rate −"),
        ("k", "fault('pod_kill')", "Kill target"),
        ("p", "fault('network_partition')", "Partition target 30s"),
    ]
    CSS = RunApp.CSS + """
    #controls, #tech-controls { height: 5; margin: 0 1; border: round $surface-lighten-2; }
    #tech-controls Button { margin: 0 1 0 0; }
    #tech-controls Select { width: 22; margin: 0 1 0 0; }
    #controls Button { margin: 0 1 0 0; min-width: 10; }
    #pods { width: 18; content-align: center middle; }
    #target { width: 24; margin: 0 1 0 0; }
    """

    def __init__(self, spec, context, session):
        super().__init__(spec, context, make_runner=lambda cb: None)  # unused
        self.session = session
        self._ended_status: str | None = None
        self._quit_requested = False
        self._tech_actions: dict[str, dict] = {}

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
            yield Button("rate −", id="rate-down")
            yield Button("rate +", id="rate-up", variant="success")
            yield Select(AUTO_TARGETS, id="target", allow_blank=False,
                         value="role:primary")
            yield Button("kill", id="kill", variant="error")
            yield Button("partition 30s", id="partition", variant="warning")
        tech_row = Horizontal(id="tech-controls")
        tech_row.border_title = "tech ops"
        tech_row.display = False  # populated from the driver's action metadata
        yield tech_row
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
        if self._quit_requested:
            self.exit(1 if status == "error" else 0)
        else:
            # ended on its own (usually an error): stay open so the message
            # can actually be read; q exits
            self.notify(f"session {status} — q to quit", severity="error" if message else "information",
                        timeout=10)

    # -- controls ---------------------------------------------------------------

    def action_scale(self, delta: int) -> None:
        self.session.scale(delta)

    def action_rate(self, direction: int) -> None:
        self.session.set_rate(direction * RATE_STEP)
        self.notify(f"per-pod rate {'+' if direction > 0 else '−'}{RATE_STEP:g} ops/s "
                    "— pool re-rolls at the new rate", timeout=5)

    def _selected_target(self) -> dict:
        value = self.query_one("#target", Select).value
        kind, _, name = str(value).partition(":")
        return {kind: name} if kind in ("role", "pod") else {"role": "primary"}

    def action_fault(self, worker: str) -> None:
        target = self._selected_target()
        duration = "30s" if worker == "network_partition" else None
        self.session.inject(worker, target, duration)
        label = target.get("pod") or f"{target.get('role')} (auto)"
        self.notify(f"{worker} on {label} requested", timeout=4)

    def action_leave(self) -> None:
        if self._ended_status is None:
            self._quit_requested = True
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
        elif button_id == "rate-up":
            self.action_rate(1)
        elif button_id == "rate-down":
            self.action_rate(-1)
        elif button_id == "kill":
            self.action_fault("pod_kill")
        elif button_id == "partition":
            self.action_fault("network_partition")
        elif button_id in self._tech_actions:
            action = self._tech_actions[button_id]
            self.session.run_action(action["id"], action["label"],
                                    self._action_params(action))
            self.notify(f"{action['label']} queued", timeout=4)

    # -- ingestion additions ------------------------------------------------------

    def _ingest(self, event: dict) -> None:
        super()._ingest(event)
        if event["type"] in ("load.scale", "load.rate"):
            self._show_pods(event.get("data", {}).get("pods", self.session.pods))
        elif event["type"] == "topology":
            self._update_targets(event.get("data", {}))
        elif event["type"] == "session.ready":
            self._mount_tech_actions(event.get("data", {}).get("actions") or [])
        elif event["type"] == "session.command.error":
            # a failed control must be seen, not buried in the event log
            self.notify(event["msg"], severity="error", timeout=8)

    def _mount_tech_actions(self, actions: list[dict]) -> None:
        """Build the tech-ops row from the driver's action metadata — the
        framework renders and dispatches; the plugin defines the semantics.
        Choice params render as a Select in front of their button (e.g. the
        PITR restore point)."""
        if not actions:
            return
        row = self.query_one("#tech-controls")
        row.display = True
        for action in actions:
            button_id = f"tech-{action['id']}"
            self._tech_actions[button_id] = action
            for param in action.get("params", []):
                select = Select(
                    [(f"{param['label']}: {option}", option) for option in param["options"]],
                    id=f"param-{action['id']}-{param['id']}",
                    allow_blank=False, value=param.get("default", param["options"][0]),
                )
                select.tooltip = param["label"]
                row.mount(select)
            button = Button(action["label"], id=button_id,
                            variant=action.get("variant", "default"))
            button.tooltip = action.get("description")
            row.mount(button)

    def _action_params(self, action: dict) -> dict | None:
        values = {
            param["id"]: self.query_one(f"#param-{action['id']}-{param['id']}", Select).value
            for param in action.get("params", [])
        }
        return values or None

    def _show_pods(self, pods: int) -> None:
        self.query_one("#pods", Static).update(Text.assemble(
            (f"{pods} pod(s)", "bold"), (f" × {self.session.rate:g} ops/s", "dim"),
            (f"\n≈{pods * self.session.rate:g} ops/s total", "dim"),
        ))

    def _update_targets(self, data: dict) -> None:
        """Keep the fault-target dropdown in step with the live topology, so a
        specific instance (pg-2, not just 'a replica') can be targeted."""
        instances = [n for n in data.get("nodes", [])
                     if n.get("role") in ("primary", "replica")]
        options = AUTO_TARGETS + [
            (f"{n['id']} ({n['role']})", f"pod:{n['id']}") for n in instances
        ]
        select = self.query_one("#target", Select)
        current = select.value
        select.set_options(options)
        if any(value == current for _, value in options):
            select.value = current
        else:
            select.value = "role:primary"  # the selected pod is gone

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
    attach: str = typer.Option(
        None, "--attach",
        help="Attach to an EXISTING namespace instead of deploying: chaos control "
             "plane mode — teardown removes only k8ost artifacts, never the namespace",
    ),
    technology: str = typer.Option(None, "--technology",
                                   help="Driver for --attach (default: auto-detect)"),
    context: str = typer.Option(None, "--context", "-c", help="Override the experiment's kubeconfig context"),
    keep: bool = typer.Option(False, "--keep", help="Leave the namespace running after the session"),
    pods: int = typer.Option(None, "--pods",
                             help="Initial load pods (default 1; 0 in attach mode — the apps drive the load)"),
    rate: float = typer.Option(20.0, "--rate", help="ops/s per load pod"),
    clients: int = typer.Option(5, "--clients", help="Clients per load pod"),
    allow_concurrent: bool = typer.Option(False, "--allow-concurrent",
                                          help="Run even if another experiment occupies the cluster"),
) -> None:
    """Interactive lab: deploy a config (or attach to a live one), scale load
    and fire faults by hand."""
    from k8ostester.core.experiment import ExperimentSpec
    from k8ostester.core.session import Session

    if not console.is_terminal:
        console.print("[red]k8ost session needs a terminal[/red] — it is interactive by nature")
        raise typer.Exit(1)
    if attach:
        if path is not None:
            console.print("[red]--attach takes no experiment directory[/red] — the cluster already exists")
            raise typer.Exit(2)
        spec = ExperimentSpec(name=f"attach-{attach}", technology=technology or "auto")
    else:
        if path is None:
            path = pick_experiment()
        spec = load_experiment(path)
    pods = pods if pods is not None else (0 if attach else 1)

    tui: SessionApp | None = None

    def on_event(event: dict) -> None:
        if tui is not None:
            tui.call_from_thread(tui._ingest, event)

    live_session = Session(spec, keep=keep, context_override=context,
                           on_event=on_event, allow_concurrent=allow_concurrent,
                           pods=pods, rate=rate, clients=clients,
                           attach_namespace=attach)
    tui = SessionApp(spec, context or spec.cluster.context, live_session)
    code = tui.run()
    raise typer.Exit(code if isinstance(code, int) else 1)