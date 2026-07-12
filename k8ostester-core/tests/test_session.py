"""Interactive session tests: the core loop driven synchronously (commands
queued up-front, stop pre-set so the loop runs exactly one pass), and the
session TUI via Textual's pilot with a fake session."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from k8ostester.core.events import EventLog
from k8ostester.core.experiment import ExperimentSpec, GoalSpec
from k8ostester.core.runner import RUN_LABEL
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
    # a tech op refreshes the action metadata (a backup opens the restore window)
    refresh = next(e for e in events if e["type"] == "session.actions")
    assert refresh["data"]["actions"] == [{"id": "backup", "label": "base backup"}]


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
                     ]}})
        await pilot.pause()
        assert app.query_one("#tech-backup", Button).label.__str__() == "base backup"

        await pilot.click("#tech-backup")
        assert fake.run_actions == [("backup", "base backup", None)]
        fake.run_actions.clear()

        # after the backup the metadata refreshes: the restore action appears
        # with the window's concrete points as dict options
        app._ingest({"type": "session.actions", "t_rel": 80.0, "msg": "tech ops refreshed",
                     "data": {"actions": [
                         {"id": "backup", "label": "base backup", "variant": "primary"},
                         {"id": "restore", "label": "restore (PITR)",
                          "params": [{"id": "target", "label": "12:00:00Z → now",
                                      "options": [
                                          {"label": "now − 1m  (12:09:00Z)", "value": "1000060"},
                                          {"label": "window start  (12:00:00Z)", "value": "1000000"},
                                      ],
                                      "default": "1000060"}]},
                     ]}})
        await pilot.pause()
        selector = app.query_one("#param-restore-target", Select)
        assert selector.value == "1000060"
        selector.value = "1000000"  # pick the window start
        await pilot.click("#tech-restore")
        assert fake.run_actions == [("restore", "restore (PITR)", {"target": "1000000"})]

        # a failed control surfaces as a notification, not just a log line
        app._ingest({"type": "session.command.error", "t_rel": 70.0,
                     "msg": "fault: network_partition needs Chaos Mesh"})

        await pilot.press("q")
    assert app.return_value == 0


@patch("k8ostester.core.session.detect_technology", return_value="postgres-cnpg")
@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_attach_mode(mock_k8s_cls, mock_get_driver, mock_detect, tmp_path):
    """Attach: discover instead of deploy, and NEVER touch the namespace."""
    from k8ostester.core.experiment import ExperimentSpec

    mock_k8s = mock_k8s_cls.return_value
    driver = mock_get_driver.return_value.return_value
    driver.stop_load_session.return_value = '{"kind": "op"}'

    spec = ExperimentSpec(name="attach-prod-db", technology="auto")
    session = Session(spec, results_root=tmp_path / "results",
                      attach_namespace="prod-db", pods=0)
    session.stop()
    session.start()

    # discovery instead of deployment
    mock_detect.assert_called_once()
    assert spec.technology == "postgres-cnpg"
    # builtin-only resolution: NO experiment dir passed, so a stray driver.py
    # in the cwd can never be exec'd against a live cluster
    mock_get_driver.assert_called_once_with("postgres-cnpg")
    driver.topology.assert_called()          # fail-fast discovery check
    driver.install_prereqs.assert_not_called()
    driver.deploy.assert_not_called()
    driver.wait_ready.assert_not_called()
    mock_k8s.create_namespace.assert_not_called()

    # load pool created inert (0 pods) so it can be dialed up later
    driver.start_load_session.assert_called_once_with(session.run_dir, 20.0, 5, 0)

    # discoverable to a later scored run: labeled on attach, reverted on teardown
    label_calls = mock_k8s.set_namespace_labels.call_args_list
    assert label_calls[0].args[0] == "prod-db"
    assert list(label_calls[0].args[1].values())[0] == session.session_id
    assert label_calls[-1].args[1] == {RUN_LABEL: None}       # reverted

    # teardown: artifacts removed, namespace untouched
    driver.stop_load_session.assert_called_once()
    mock_k8s.delete_namespace.assert_not_called()

    events = EventLog.read(session.run_dir / "events.jsonl")
    types = [e["type"] for e in events]
    assert "session.detect" in types and "session.attach" in types
    skip = next(e for e in events if e["type"] == "teardown.skip")
    assert "untouched" in skip["msg"]


@patch("k8ostester.core.session.detect_technology", return_value=None)
@patch("k8ostester.core.session.ClusterClient")
def test_session_attach_detection_failure(mock_k8s_cls, mock_detect, tmp_path):
    from k8ostester.core.experiment import ExperimentSpec
    from k8ostester.core.exceptions import K8osInfraError

    spec = ExperimentSpec(name="attach-empty", technology="auto")
    session = Session(spec, results_root=tmp_path / "results",
                      attach_namespace="empty-ns", pods=0)
    with pytest.raises(K8osInfraError, match="no supported technology detected"):
        session.start()
    mock_k8s_cls.return_value.delete_namespace.assert_not_called()  # never ours


def test_session_command_attach_wiring(tmp_path):
    from unittest.mock import PropertyMock
    from rich.console import Console
    from typer.testing import CliRunner
    from k8ostester.cli import app as cli_app

    with patch("k8ostester.core.session.Session") as mock_session_cls, \
         patch("k8ostester.cli.session.SessionApp") as mock_app_cls, \
         patch.object(Console, "is_terminal", new_callable=PropertyMock, return_value=True):
        mock_app_cls.return_value.run.return_value = 0
        result = CliRunner().invoke(cli_app, ["session", "--attach", "prod-db"])
        assert result.exit_code == 0
        kwargs = mock_session_cls.call_args[1]
        assert kwargs["attach_namespace"] == "prod-db"
        assert kwargs["pods"] == 0                      # attach default: observe only
        spec = mock_session_cls.call_args[0][0]
        assert spec.name == "attach-prod-db"
        assert spec.technology == "auto"

        # attach + experiment dir is a contradiction
        result = CliRunner().invoke(cli_app, ["session", str(tmp_path), "--attach", "prod-db"])
        assert result.exit_code == 2


@patch("k8ostester.core.session.get_worker")
@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_recorder_exports_replayable_experiment(mock_k8s_cls, mock_get_driver, mock_get_worker, spec, tmp_path):
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.stop_load_session.return_value = ""

    session = make_session(spec, tmp_path, pods=1, rate=20.0, clients=5)
    session.scale(1)
    session.inject("pod_kill", {"role": "primary"})
    session.stop()
    session.start()

    recorded = session.run_dir / "recorded"
    assert (recorded / "experiment.yaml").exists()
    assert (recorded / "manifests").is_dir()  # copied from the source experiment
    import yaml
    doc = yaml.safe_load((recorded / "experiment.yaml").read_text())
    assert doc["name"] == "lab-recorded"
    assert doc["faults"][0]["worker"] == "pod_kill"
    assert doc["faults"][0]["target"] == {"role": "primary"}
    assert doc["verify"] == ["integrity"]
    assert doc["goals"] == [{"metric": "uptime", "min": "98%"}]

    events = EventLog.read(session.run_dir / "events.jsonl")
    assert any(e["type"] == "session.recorded" for e in events)


def test_recorded_spec_timeline(spec, tmp_path):
    """The pure timeline synthesis: scale/rate changes become load phases,
    faults keep offsets, a backup adds the verification."""
    from k8ostester.core.experiment import ExperimentSpec

    session = make_session(spec, tmp_path, pods=1, rate=20.0, clients=5)
    t0 = 1000.0
    session._ready_at = t0
    session._ready_state = (1, 20.0, 5)
    session._recorded = [
        (t0 + 30, ("pods", 3)),                                # scale up at +30s
        (t0 + 60, ("fault", "pod_kill", {"pod": "pg-3"}, None)),
        (t0 + 75, ("fault", "network_partition", {"role": "primary"}, "30s")),
        (t0 + 90, ("rate", 50.0)),                             # rate change at +90s
        (t0 + 100, ("backup",)),
        (t0 + 120, ("pods", 0)),                               # pause at +120s
    ]
    doc = session.recorded_spec(end=t0 + 150)

    assert doc["load"]["phases"] == [
        {"duration": "30s", "rate": "20/s", "clients": {"count": 5, "mode": "persistent"}},
        {"duration": "60s", "rate": "60/s", "clients": {"count": 15, "mode": "persistent"}},
        {"duration": "30s", "rate": "150/s", "clients": {"count": 15, "mode": "persistent"}},
        {"duration": "30s", "rate": "0/s"},
    ]
    assert doc["faults"] == [
        {"at": "60s", "worker": "pod_kill", "target": {"pod": "pg-3"}},
        {"at": "75s", "worker": "network_partition", "target": {"role": "primary"},
         "duration": "30s"},
    ]
    assert doc["verify"] == ["integrity", "backup"]
    # the export round-trips through the spec model
    ExperimentSpec.model_validate(doc)


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_attach_honors_concurrent_run_guard(mock_k8s_cls, mock_get_driver, tmp_path):
    """Attach must not silently corrupt a scored run already on the cluster."""
    from k8ostester.core.exceptions import K8osInfraError

    mock_k8s = mock_k8s_cls.return_value
    occupied = MagicMock()
    occupied.metadata.name = "exp-something-live"
    mock_k8s.core.list_namespace.return_value.items = [occupied]

    spec = ExperimentSpec(name="attach-prod", technology="postgres-cnpg")
    session = Session(spec, results_root=tmp_path / "results",
                      attach_namespace="prod-db", pods=0)
    with pytest.raises(K8osInfraError, match="already occupies this cluster"):
        session.start()
    mock_get_driver.return_value.return_value.start_load_session.assert_not_called()


@patch("k8ostester.core.session.detect_technology", return_value="postgres-cnpg")
@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_attach_teardown_reports_cleanup_failure(mock_k8s_cls, mock_get_driver, mock_detect, tmp_path):
    """When artifact removal fails, teardown must NOT claim success."""
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.stop_load_session.return_value = ""
    driver.cleanup_failures = ["deployment/k8ost-loadgen"]

    spec = ExperimentSpec(name="attach-prod-db", technology="auto")
    session = Session(spec, results_root=tmp_path / "results",
                      attach_namespace="prod-db", pods=0)
    session.stop()
    session.start()

    events = EventLog.read(session.run_dir / "events.jsonl")
    errors = [e for e in events if e["type"] == "teardown.error"]
    assert any("deployment/k8ost-loadgen" in e["msg"] for e in errors)
    assert any("could NOT be removed" in e["msg"] for e in errors)
    assert not any(e["type"] == "teardown.skip" for e in events)  # no false success


@patch("k8ostester.core.session.get_driver")
@patch("k8ostester.core.session.ClusterClient")
def test_session_tech_ops_serialized(mock_k8s_cls, mock_get_driver, spec, tmp_path):
    """A long-running tech op runs off the loop thread; a second op while one
    holds the lock is rejected rather than queued behind a 10-minute wait."""
    mock_k8s_cls.return_value.core.list_namespace.return_value.items = []
    driver = mock_get_driver.return_value.return_value
    driver.session_actions.return_value = []
    driver.stop_load_session.return_value = ""

    release = threading.Event()
    started = threading.Event()

    def slow_action(action_id, params):
        started.set()
        assert release.wait(timeout=5)
        return "done"
    driver.run_session_action.side_effect = slow_action

    session = make_session(spec, tmp_path)
    session._ready_at = 1.0  # let it act like a live session for dispatch

    # drive the two dispatches directly (no real loop needed)
    session._start_tech_action(driver, "backup", "base backup", None)
    assert started.wait(timeout=5)                 # first op running on its thread
    session._start_tech_action(driver, "backup", "base backup", None)  # lock held
    release.set()
    import time as _t
    _t.sleep(0.2)

    # exactly one action actually ran; the second was rejected with an error
    assert driver.run_session_action.call_count == 1
