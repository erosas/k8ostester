"""Interactive session tests: the core loop driven synchronously (commands
queued up-front, stop pre-set so the loop runs exactly one pass), and the
session TUI via Textual's pilot with a fake session."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from k8ostester.core.events import EventLog
from k8ostester.core.experiment import ExperimentSpec, GoalSpec
from k8ostester.core.session import Session


@pytest.fixture
def spec(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    (d / "manifests").mkdir()
    return ExperimentSpec(name="lab", technology="postgres-cnpg", dir=d,
                          goals=[GoalSpec(metric="uptime", min="98%")])


def make_session(spec, tmp_path, **kwargs):
    return Session(spec, results_root=tmp_path / "results", **kwargs)


@patch("k8ostester.core.session.get_worker")
@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_lifecycle(mock_k8s_cls, mock_get_driver, mock_get_worker, spec, tmp_path):
    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.stop_load_session.return_value = '{"kind": "op"}'

    session = make_session(spec, tmp_path, pods=1, rate=10.0, clients=3)
    session.scale(1)                                  # queued before the loop
    session.inject("pod_kill", {"role": "primary"})
    session.stop()                                    # loop runs exactly one pass

    session.start()

    driver.install_prereqs.assert_called_once()
    driver.deploy.assert_called_once()
    driver.wait_ready.assert_called_once()
    driver.start_load_session.assert_called_once_with(session.run_dir, 10.0, 3, 1)
    driver.scale_load.assert_called_once_with(2)      # 1 + 1
    assert session.pods == 2
    mock_get_worker.assert_called_once_with("pod_kill")
    mock_get_worker.return_value.return_value.execute.assert_called_once()
    driver.emit_live_telemetry.assert_called()

    # teardown: pool logs kept, namespace deleted, summary written
    assert (session.run_dir / "loadgen.log").read_text() == '{"kind": "op"}'
    mock_k8s.delete_namespace.assert_called_once()
    summary = json.loads((session.run_dir / "summary.json").read_text())
    assert summary["status"] == "session"

    events = [e["type"] for e in EventLog.read(session.run_dir / "events.jsonl")]
    assert "session.ready" in events
    assert "load.scale" in events
    assert "fault.injected" in events


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_scale_clamps_at_zero(mock_k8s_cls, mock_get_driver, spec, tmp_path):
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.stop_load_session.return_value = ""

    session = make_session(spec, tmp_path, pods=1)
    session.scale(-5)
    session.stop()
    session.start()

    driver.scale_load.assert_called_once_with(0)  # 0 pods = paused load
    assert session.pods == 0


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_command_failure_is_reported_not_fatal(mock_k8s_cls, mock_get_driver, spec, tmp_path):
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.scale_load.side_effect = RuntimeError("deployment not found")
    driver.stop_load_session.return_value = ""

    session = make_session(spec, tmp_path)
    session.scale(1)
    session.stop()
    session.start()  # must not raise

    events = EventLog.read(session.run_dir / "events.jsonl")
    assert any(e["type"] == "session.command.error" for e in events)


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_setup_error_tears_down(mock_k8s_cls, mock_get_driver, spec, tmp_path):
    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.deploy.side_effect = RuntimeError("manifests broken")
    driver.stop_load_session.return_value = ""

    session = make_session(spec, tmp_path)
    with pytest.raises(RuntimeError, match="manifests broken"):
        session.start()

    mock_k8s.delete_namespace.assert_called_once()
    summary = json.loads((session.run_dir / "summary.json").read_text())
    assert summary["status"] == "error"
    assert "manifests broken" in summary["error"]


# -- the session TUI -------------------------------------------------------------


class FakeSession:
    pods = 1
    rate = 20.0

    def __init__(self):
        self._stop = threading.Event()
        self.calls = []

    def start(self):
        assert self._stop.wait(timeout=10), "session was never stopped"

    def scale(self, delta):
        self.calls.append(("scale", delta))

    def set_rate(self, delta):
        self.calls.append(("rate", delta))

    def inject(self, worker, target, duration=None):
        self.calls.append(("fault", worker, target, duration))

    def stop(self):
        self._stop.set()


async def test_session_app_controls(spec):
    from textual.widgets import Select, Static
    from k8ostester.cli.session import SessionApp

    fake = FakeSession()
    app = SessionApp(spec, "docker-desktop", fake)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("plus")
        await pilot.press("minus")
        await pilot.press("right_square_bracket")
        await pilot.press("left_square_bracket")
        await pilot.press("k")   # kill the selected target (default: primary auto)
        await pilot.press("p")   # partition the selected target
        assert fake.calls == [
            ("scale", 1), ("scale", -1),
            ("rate", 5.0), ("rate", -5.0),
            ("fault", "pod_kill", {"role": "primary"}, None),
            ("fault", "network_partition", {"role": "primary"}, "30s"),
        ]
        fake.calls.clear()

        # topology feeds the target dropdown with the live instances
        app._ingest({"type": "topology", "t_rel": 30.0, "msg": "primary pg-1",
                     "data": {"primary": "pg-1", "replicas": ["pg-2"],
                              "nodes": [{"id": "pg-1", "role": "primary"},
                                        {"id": "pg-2", "role": "replica"}],
                              "edges": []}})
        select = app.query_one("#target", Select)
        assert ("pg-2 (replica)", "pod:pg-2") in list(select._options)

        # picking a specific instance targets exactly that pod
        select.value = "pod:pg-2"
        await pilot.press("k")
        assert fake.calls == [("fault", "pod_kill", {"pod": "pg-2"}, None)]

        # a load.scale event updates the pods indicator
        app._ingest({"type": "load.scale", "t_rel": 5.0, "msg": "3 load pod(s)",
                     "data": {"pods": 3}})
        assert "3" in str(app.query_one("#pods", Static).content)

        await pilot.press("q")  # stop → session loop returns → app exits

    assert app.return_value == 0


def test_session_command_requires_terminal(tmp_path):
    from typer.testing import CliRunner
    from k8ostester.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["session", str(tmp_path)])
    assert result.exit_code == 1
    assert "needs a terminal" in result.output

class ExplodingSession(FakeSession):
    def start(self):
        raise RuntimeError("cluster unreachable")


async def test_session_app_error_stays_open_until_quit(spec):
    """An error must be readable, not a flash before exit — the app stays up
    and q exits with code 1 (regression: the 'crash' was an instant exit)."""
    from k8ostester.cli.session import SessionApp

    app = SessionApp(spec, None, ExplodingSession())
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.error == "cluster unreachable"
        assert app.return_value is None  # still open, error on screen
        await pilot.press("q")
    assert app.return_value == 1


def test_session_command_wiring(tmp_path):
    from unittest.mock import PropertyMock
    from rich.console import Console
    from typer.testing import CliRunner
    from k8ostester.cli import app as cli_app

    exp_dir = tmp_path / "lab"
    exp_dir.mkdir()
    (exp_dir / "experiment.yaml").write_text("name: lab\ntechnology: postgres-cnpg\n")
    (exp_dir / "manifests").mkdir()

    with patch("k8ostester.core.session.Session") as mock_session_cls, \
         patch("k8ostester.cli.session.SessionApp") as mock_app_cls, \
         patch.object(Console, "is_terminal", new_callable=PropertyMock, return_value=True):
        mock_app_cls.return_value.run.return_value = 0
        result = CliRunner().invoke(cli_app, [
            "session", str(exp_dir), "--pods", "2", "--rate", "50", "--clients", "8"])
        assert result.exit_code == 0
        assert mock_session_cls.call_args[1]["pods"] == 2
        assert mock_session_cls.call_args[1]["rate"] == 50.0
        assert mock_session_cls.call_args[1]["clients"] == 8


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_rate_change(mock_k8s_cls, mock_get_driver, spec, tmp_path):
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.stop_load_session.return_value = ""

    session = make_session(spec, tmp_path, pods=2, rate=20.0, clients=5)
    session.set_rate(5.0)
    session.set_rate(-100.0)  # clamps at 1 ops/s
    session.stop()
    session.start()

    assert driver.set_load_rate.call_args_list[0][0] == (25.0, 5)
    assert driver.set_load_rate.call_args_list[1][0] == (1.0, 5)
    events = EventLog.read(session.run_dir / "events.jsonl")
    assert sum(e["type"] == "load.rate" for e in events) == 2


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_tech_action_dispatch(mock_k8s_cls, mock_get_driver, spec, tmp_path):
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.session_actions.return_value = [{"id": "backup", "label": "base backup"}]
    driver.run_session_action.return_value = "base backup k8ost-1 completed"
    driver.stop_load_session.return_value = ""

    session = make_session(spec, tmp_path)
    session.run_action("backup", "base backup")
    session.stop()
    session.start()

    driver.run_session_action.assert_called_once_with("backup", None)
    events = EventLog.read(session.run_dir / "events.jsonl")
    ready = next(e for e in events if e["type"] == "session.ready")
    assert ready["data"]["actions"] == [{"id": "backup", "label": "base backup"}]
    action_events = [e for e in events if e["type"] == "session.action"]
    assert len(action_events) == 2  # running… + summary
    assert "completed" in action_events[1]["msg"]


async def test_session_app_mounts_tech_actions(spec):
    from textual.widgets import Button
    from k8ostester.cli.session import SessionApp

    from textual.widgets import Select

    fake = FakeSession()
    fake.run_actions = []
    fake.run_action = lambda aid, label, params=None: fake.run_actions.append((aid, label, params))
    app = SessionApp(spec, None, fake)

    async with app.run_test(size=(160, 44)) as pilot:
        app._ingest({"type": "session.ready", "t_rel": 60.0, "msg": "controls live",
                     "data": {"actions": [
                         {"id": "backup", "label": "base backup", "variant": "primary"},
                         {"id": "pitr-drill", "label": "PITR drill",
                          "params": [{"id": "minutes_ago", "label": "restore to",
                                      "options": ["1", "2", "5"], "default": "2"}]},
                     ]}})
        await pilot.pause()
        assert app.query_one("#tech-backup", Button).label.__str__() == "base backup"

        await pilot.click("#tech-backup")
        assert fake.run_actions == [("backup", "base backup", None)]
        fake.run_actions.clear()

        # the PITR time selector feeds the action's params
        selector = app.query_one("#param-pitr-drill-minutes_ago", Select)
        assert selector.value == "2"
        selector.value = "5"
        await pilot.click("#tech-pitr-drill")
        assert fake.run_actions == [("pitr-drill", "PITR drill", {"minutes_ago": "5"})]

        # a failed control surfaces as a notification, not just a log line
        app._ingest({"type": "session.command.error", "t_rel": 70.0,
                     "msg": "fault: network_partition needs Chaos Mesh"})

        await pilot.press("q")
    assert app.return_value == 0
