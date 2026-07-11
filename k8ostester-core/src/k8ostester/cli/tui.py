"""Full-screen TUI for `k8ost run --tui`.

The live panel grown into an app with drill-in views — Overview, Metrics,
Topology, Events — switched with key bindings while the run executes on a
worker thread. The runner feeds the app through the same event stream as the
plain and live outputs; when the run finishes the app stays open for
inspection and `q` exits with the run's exit code (0 passed / 1 error /
2 failed).
"""

from __future__ import annotations

import time
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    DataTable, Footer, ProgressBar, RichLog, Sparkline, Static,
    TabbedContent, TabPane,
)

from k8ostester.cli.live import ALERT_TYPES, PANE_TYPES
from k8ostester.cli.run import verdict_table
from k8ostester.core.experiment import ExperimentSpec

STATUS_STYLE = {"running": "cyan", "passed": "green", "failed": "red", "error": "red"}
# goals only the final evaluation can score
END_OF_RUN = Text("end of run", style="dim")


def _event_line(event: dict) -> Text:
    alert = event["type"] in ALERT_TYPES
    return Text.assemble(
        (f"{event['t_rel']:>8.1f}s ", "dim"),
        (f"{event['type']:<18} ", "bold red" if alert else "bold"),
        (event["msg"], "red" if alert else ""),
    )


class RunApp(App):
    """One experiment run, live."""

    TITLE = "k8ost run"
    BINDINGS = [
        ("o", "show_tab('tab-overview')", "Overview"),
        ("m", "show_tab('tab-metrics')", "Metrics"),
        ("t", "show_tab('tab-topology')", "Topology"),
        ("e", "show_tab('tab-events')", "Events"),
        ("q", "leave", "Quit"),
    ]
    CSS = """
    #header { height: 1; padding: 0 1; background: $panel; }
    #load-progress { margin: 0 1; }
    #ov-panes { height: auto; }
    #ov-metrics, #ov-topology { width: 1fr; padding: 0 1; }
    #ov-tail { height: 1fr; padding: 0 1; }
    #ov-verdict { padding: 0 1; }
    #ops-spark { height: 3; margin: 0 1; }
    #m-rates { padding: 0 1; }
    #t-current { padding: 1; }
    TabPane { padding: 0; }
    """

    def __init__(self, spec: ExperimentSpec, context: str | None,
                 make_runner: Callable[[Callable[[dict], None]], object]):
        super().__init__()
        self.spec = spec
        self.context = context or "(current)"
        self._make_runner = make_runner
        self.result = None
        self.error: str | None = None
        self.started = time.time()
        self.current_step = "starting"
        self.status = "running"
        self.sample: dict | None = None
        self.ops_history: list[float] = []
        self.load_total_s: float | None = None
        self.load_started_at: float | None = None
        self._tail_events: list[dict] = []
        self._exit_code = 1  # interrupted before a verdict counts as an error

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with TabbedContent(initial="tab-overview"):
            with TabPane("Overview", id="tab-overview"):
                yield ProgressBar(id="load-progress", show_eta=False)
                with Horizontal(id="ov-panes"):
                    yield Static(id="ov-metrics")
                    yield Static(id="ov-topology")
                yield Static(id="ov-verdict")
                with VerticalScroll(id="ov-tail-scroll"):
                    yield Static(id="ov-tail")
            with TabPane("Metrics", id="tab-metrics"):
                yield Sparkline([], summary_function=max, id="ops-spark")
                yield Static(id="m-rates")
                yield DataTable(id="m-goals", cursor_type="row")
            with TabPane("Topology", id="tab-topology"):
                yield Static(id="t-current")
                yield RichLog(id="t-history", markup=False, highlight=False)
            with TabPane("Events", id="tab-events"):
                yield RichLog(id="e-log", markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#load-progress").display = False
        table = self.query_one("#m-goals", DataTable)
        table.add_columns("", "goal / check", "value", "threshold", "detail")
        self._seed_goal_rows()
        self._tick()
        self.set_interval(0.5, self._tick)
        self.run_worker(self._execute, thread=True)

    def _seed_goal_rows(self) -> None:
        table = self.query_one("#m-goals", DataTable)
        for step in self.spec.verify:
            name = step if isinstance(step, str) else next(iter(step))
            table.add_row(Text("·"), f"verify:{name}", END_OF_RUN, "must pass", "", key=f"verify:{name}")
        for g in self.spec.goals:
            name = g.metric or g.check
            threshold = f"max {g.max}" if g.max is not None else f"min {g.min}" if g.min is not None else "must pass"
            table.add_row(Text("·"), name, END_OF_RUN, threshold, "", key=name)

    # -- the run (worker thread) --------------------------------------------

    def _execute(self) -> None:
        runner = self._make_runner(
            lambda event: self.call_from_thread(self._ingest, event)
        )
        try:
            result = runner.run()
        except Exception as e:
            self.call_from_thread(self._finish_error, str(e))
        else:
            self.call_from_thread(self._finish, result)

    # -- event ingestion (main thread) ----------------------------------------

    def _ingest(self, event: dict) -> None:
        etype = event["type"]
        data = event.get("data", {})
        if etype not in PANE_TYPES:
            self.current_step = etype
            self.query_one("#e-log", RichLog).write(_event_line(event))
            self._append_tail(event)
        if etype == "load.start":
            self.load_total_s = data.get("total_s")
            self.load_started_at = time.time()
            self.query_one("#load-progress").display = bool(self.load_total_s)
        elif etype == "load.sample":
            self.sample = data
            self.ops_history = (self.ops_history + [data["ops_s"]])[-120:]
            self.query_one("#ops-spark", Sparkline).data = self.ops_history
            self._update_metrics()
        elif etype == "topology":
            self._update_topology(event)
        elif etype == "fault.injected":
            self.query_one("#t-history", RichLog).write(_event_line(event))

    def _append_tail(self, event: dict) -> None:
        self._tail_events = (self._tail_events + [event])[-10:]
        self.query_one("#ov-tail", Static).update(
            Text("\n").join(_event_line(e) for e in self._tail_events)
        )

    def _update_metrics(self) -> None:
        s = self.sample or {}
        rates = Text.assemble(
            (f"{s.get('ops_s', 0):.1f}", "bold green"), (" ops/s   ", "dim"),
            (f"{s.get('err_s', 0):.1f}", "bold red" if s.get("err_s") else "bold"), (" err/s   ", "dim"),
            (f"{s.get('total_ops', 0)} ops · {s.get('acked_writes', 0)} acked writes · "
             f"{s.get('failed', 0)} failed", "dim"),
        )
        self.query_one("#m-rates", Static).update(rates)
        self.query_one("#ov-metrics", Static).update(rates)
        table = self.query_one("#m-goals", DataTable)
        for g in s.get("goals", []):
            self._set_goal_row(table, g)

    def _set_goal_row(self, table: DataTable, g: dict) -> None:
        key = g["goal"] if not g.get("check") else f"verify:{g['goal']}"
        try:
            table.update_cell(key, table.ordered_columns[0].key,
                              Text("✔", style="green") if g["passed"] else Text("✘", style="red"))
            table.update_cell(key, table.ordered_columns[2].key, Text(str(g["value"]), style="bold"))
            table.update_cell(key, table.ordered_columns[4].key, g.get("detail", ""))
        except Exception:
            pass  # a goal the seed didn't anticipate — skip rather than crash

    def _update_topology(self, event: dict) -> None:
        data = event.get("data", {})
        lines = []
        if data.get("primary"):
            lines.append(Text.assemble(("● ", "bold green"), (data["primary"], "bold"), ("  primary", "dim")))
        for replica in data.get("replicas", []):
            lines.append(Text.assemble(("○ ", "cyan"), replica, ("  replica", "dim")))
        rendered = Text("\n").join(lines)
        self.query_one("#t-current", Static).update(rendered)
        self.query_one("#ov-topology", Static).update(rendered)
        self.query_one("#t-history", RichLog).write(Text.assemble(
            (f"{event['t_rel']:>8.1f}s ", "dim"), ("primary → ", ""),
            (str(data.get("primary")), "bold"),
            (f"  (replicas: {', '.join(data.get('replicas', [])) or '—'})", "dim"),
        ))

    # -- completion -----------------------------------------------------------

    def _finish(self, result) -> None:
        self.result = result
        self.status = result.status
        self._exit_code = {"passed": 0, "failed": 2}.get(result.status, 1)
        self.query_one("#ov-verdict", Static).update(verdict_table(result))
        table = self.query_one("#m-goals", DataTable)
        for v in result.verifications:
            self._set_goal_row(table, {"goal": v["check"], "check": True, "passed": v["passed"],
                                       "value": "pass" if v["passed"] else "fail",
                                       "detail": v["detail"]})
        for g in result.goals:
            self._set_goal_row(table, g)
        self._tick()
        self.notify(f"run {result.status} — results: {result.run_dir}",
                    severity="information" if result.status == "passed" else "error",
                    timeout=10)

    def _finish_error(self, message: str) -> None:
        self.error = message
        self.status = "error"
        self._exit_code = 1
        self.query_one("#e-log", RichLog).write(Text(f"run error: {message}", style="bold red"))
        self.query_one("#ov-verdict", Static).update(Text(f"run error: {message}", style="bold red"))
        self._tick()
        self.notify(f"run error: {message}", severity="error", timeout=10)

    # -- chrome -----------------------------------------------------------------

    def _tick(self) -> None:
        running = self.result is None and self.error is None
        elapsed = time.time() - self.started
        self.query_one("#header", Static).update(Text.assemble(
            (f"{self.spec.name} ({self.spec.technology})", "bold"),
            (f"  context {self.context}", "dim"),
            (f"  {elapsed:6.1f}s  " if running else "  ", "dim"),
            (self.status if not running else self.current_step,
             STATUS_STYLE.get(self.status, "cyan") if not running else "cyan"),
            ("  (q to quit)" if not running else "", "dim"),
        ))
        if running and self.load_total_s and self.load_started_at:
            self.query_one("#load-progress", ProgressBar).update(
                total=self.load_total_s,
                progress=min(time.time() - self.load_started_at, self.load_total_s),
            )

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_leave(self) -> None:
        self.exit(self._exit_code)


def run_tui(spec: ExperimentSpec, *, keep: bool, context: str | None,
            group: str | None, allow_concurrent: bool) -> int:
    """Run one experiment under the TUI; returns the process exit code."""
    from k8ostester.core.runner import Runner

    def make_runner(on_event):
        return Runner(spec, keep=keep, context_override=context, group_override=group,
                      on_event=on_event, allow_concurrent=allow_concurrent)

    app = RunApp(spec, context or spec.cluster.context, make_runner)
    code = app.run()
    return code if isinstance(code, int) else 1