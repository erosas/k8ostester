"""TUI tests via Textual's headless pilot: a fake runner feeds the app the
same event stream a real run produces."""

from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable, RichLog, Sparkline, TabbedContent

from k8ostester.cli.tui import RunApp
from k8ostester.core.experiment import ExperimentSpec, GoalSpec
from k8ostester.core.runner import RunResult

EVENTS = [
    {"type": "run.start", "t_rel": 0.0, "msg": "experiment demo"},
    {"type": "load.start", "t_rel": 20.0, "msg": "1 phase(s), ~150s", "data": {"total_s": 150.0}},
    {"type": "load.sample", "t_rel": 30.0, "msg": "19.8 ops/s",
     "data": {"ops_s": 19.8, "err_s": 0.2, "total_ops": 400, "acked_writes": 280, "failed": 2,
              "goals": [{"goal": "uptime", "value": "99.40%", "threshold": "min 98",
                         "passed": True, "detail": "so far"}]}},
    {"type": "topology", "t_rel": 30.0, "msg": "primary pg-1",
     "data": {"primary": "pg-1", "replicas": ["pg-2", "pg-3"]}},
    {"type": "fault.injected", "t_rel": 80.0, "msg": "pod_kill at +60s"},
    {"type": "topology", "t_rel": 86.0, "msg": "primary pg-2",
     "data": {"primary": "pg-2", "replicas": ["pg-1", "pg-3"]}},
]


def make_spec() -> ExperimentSpec:
    return ExperimentSpec(
        name="demo", technology="postgres-cnpg",
        verify=["integrity"],
        goals=[GoalSpec(metric="uptime", min="98%"), GoalSpec(metric="rto", max="10s")],
    )


class FakeRunner:
    def __init__(self, on_event, status="passed", error=None):
        self.on_event = on_event
        self.status = status
        self.error = error

    def run(self):
        for event in EVENTS:
            self.on_event(event)
        if self.error:
            raise RuntimeError(self.error)
        result = RunResult("r1", Path("results/demo/r1"))
        result.status = self.status
        result.verifications = [{"check": "integrity", "passed": True, "detail": "all present"}]
        result.goals = [
            {"goal": "uptime", "value": "99.40%", "threshold": "min 98%", "passed": True, "detail": ""},
            {"goal": "rto", "value": "1.6s", "threshold": "max 10s", "passed": True, "detail": ""},
        ]
        return result


async def test_tui_full_run_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # RunResult path handling stays local
    app = RunApp(make_spec(), "docker-desktop", lambda cb: FakeRunner(cb))

    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.result.status == "passed"
        assert app.status == "passed"
        assert app.ops_history == [19.8]
        assert app.query_one("#ops-spark", Sparkline).data == [19.8]

        # goal table: live + final values landed, rto filled at the end
        table = app.query_one("#m-goals", DataTable)
        assert table.row_count == 3  # verify:integrity, uptime, rto
        assert str(table.get_row("rto")[2]) == "1.6s"
        assert str(table.get_row("verify:integrity")[2]) == "pass"

        # drill-in bindings switch tabs
        tabs = app.query_one(TabbedContent)
        assert tabs.active == "tab-overview"
        await pilot.press("m")
        assert tabs.active == "tab-metrics"
        await pilot.press("t")
        assert tabs.active == "tab-topology"

        # topology history recorded both primaries + the fault (RichLog only
        # renders its buffer once the tab is visible)
        await pilot.pause()
        history = app.query_one("#t-history", RichLog)
        assert len(history.lines) >= 3

        await pilot.press("e")
        assert tabs.active == "tab-events"
        await pilot.press("o")
        assert tabs.active == "tab-overview"

        await pilot.press("q")

    assert app.return_value == 0


async def test_tui_failed_run_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = RunApp(make_spec(), None, lambda cb: FakeRunner(cb, status="failed"))
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.status == "failed"
        await pilot.press("q")
    assert app.return_value == 2


async def test_tui_run_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = RunApp(make_spec(), None, lambda cb: FakeRunner(cb, error="cluster gone"))
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.status == "error"
        assert app.error == "cluster gone"
        await pilot.press("q")
    assert app.return_value == 1


def test_run_command_tui_flag(tmp_path):
    from typer.testing import CliRunner
    from k8ostester.cli import app as cli_app

    exp_dir = tmp_path / "my-exp"
    exp_dir.mkdir()
    (exp_dir / "experiment.yaml").write_text("name: dummy\ntechnology: generic\n")
    (exp_dir / "manifests").mkdir()

    with patch("k8ostester.cli.tui.run_tui", return_value=2) as mock_tui:
        result = CliRunner().invoke(cli_app, ["run", str(exp_dir), "--tui"])
        assert result.exit_code == 2
        assert mock_tui.call_args[1]["keep"] is False