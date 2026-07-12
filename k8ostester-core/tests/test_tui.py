"""TUI tests via Textual's headless pilot: a fake runner feeds the app the
same event stream a real run produces."""

from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable, RichLog, Static

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
    {"type": "topology", "t_rel": 30.0, "msg": "primary pg-1 · pg-2 sync · pg-3 async",
     "data": {"primary": "pg-1", "replicas": ["pg-2", "pg-3"],
              "nodes": [{"id": "loadgen", "role": "client", "detail": "5 clients, persistent"},
                        {"id": "pg-1", "role": "primary", "detail": "healthy"},
                        {"id": "pg-2", "role": "replica", "detail": "healthy"},
                        {"id": "pg-3", "role": "replica", "detail": "healthy"}],
              "edges": [{"source": "loadgen", "target": "pg-1", "detail": "pg-rw"},
                        {"source": "pg-1", "target": "pg-2", "detail": "sync"},
                        {"source": "pg-1", "target": "pg-3", "detail": "async"}]}},
    {"type": "fault.injected", "t_rel": 80.0, "msg": "pod_kill at +60s"},
    {"type": "topology", "t_rel": 86.0, "msg": "primary pg-2 · pg-1 detached · pg-3 async",
     "data": {"primary": "pg-2", "replicas": ["pg-1", "pg-3"],
              "nodes": [{"id": "loadgen", "role": "client", "detail": "5 clients, persistent"},
                        {"id": "pg-2", "role": "primary", "detail": "healthy"},
                        {"id": "pg-1", "role": "replica", "detail": "failed"},
                        {"id": "pg-3", "role": "replica", "detail": "healthy"}],
              "edges": [{"source": "loadgen", "target": "pg-2", "detail": "pg-rw"},
                        {"source": "pg-2", "target": "pg-1", "detail": "detached"},
                        {"source": "pg-2", "target": "pg-3", "detail": "async"}]}},
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
        rates = str(app.query_one("#m-rates", Static).content)
        assert "19.8" in rates and "0.50%" in rates  # 2 failed of 400 ops

        # goal table: live + final values landed, rto filled at the end
        table = app.query_one("#m-goals", DataTable)
        assert table.row_count == 3  # verify:integrity, uptime, rto
        assert str(table.get_row("rto")[2]) == "1.6s"
        assert str(table.get_row("verify:integrity")[2]) == "pass"

        # single-page dashboard: topology history is visible alongside
        # everything else — both primaries plus the fault are recorded
        history = app.query_one("#t-history", RichLog)
        assert len(history.lines) >= 3

        # the current pane renders the connection tree with replication modes
        current = str(app.query_one("#t-current", Static).content)
        assert "loadgen" in current and "pg-2" in current
        assert "detached" in current and "async" in current

        # events log is on the same page and populated
        assert len(app.query_one("#e-log", RichLog).lines) > 0

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
        result = CliRunner().invoke(cli_app, ["run", str(exp_dir), "--view", "tui"])
        assert result.exit_code == 2
        assert mock_tui.call_args[1]["keep"] is False

async def test_tui_quit_blocked_while_run_in_flight(tmp_path, monkeypatch):
    """q mid-run must not abandon the runner worker: teardown would be
    skipped and the namespace leaked (regression)."""
    import threading
    monkeypatch.chdir(tmp_path)

    release = threading.Event()

    class SlowRunner:
        def __init__(self, on_event):
            self.on_event = on_event

        def run(self):
            assert release.wait(timeout=10)
            result = RunResult("r1", Path("results/demo/r1"))
            result.status = "passed"
            return result

    app = RunApp(make_spec(), None, lambda cb: SlowRunner(cb))
    async with app.run_test() as pilot:
        await pilot.press("q")           # in flight: blocked with a warning
        assert app.return_value is None  # still running
        release.set()                    # let the run finish
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.status == "passed"
        await pilot.press("q")           # now it exits
    assert app.return_value == 0
