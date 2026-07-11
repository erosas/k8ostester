import pytest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY
from k8ostester.core.events import EventLog
from k8ostester.core.exceptions import K8osConfigError, K8osInfraError
from k8ostester.core.runner import Runner, RunResult
from k8ostester.core.experiment import ExperimentSpec, GoalSpec, LoadSpec, FaultSpec, ClusterSpec

@pytest.fixture
def dummy_spec(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    (d / "manifests").mkdir()
    return ExperimentSpec(
        name="dummy",
        technology="generic",
        dir=d,
        cluster=ClusterSpec(context="test-ctx"),
        verify=[],
        goals=[]
    )

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_basic_flow(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = [] # No concurrent runs
    
    mock_driver_cls = MagicMock()
    mock_get_driver.return_value = mock_driver_cls
    mock_driver = mock_driver_cls.return_value
    
    runner = Runner(dummy_spec, results_root=tmp_path)
    res = runner.run()
    
    assert res.status == "passed"
    assert res.namespace.startswith("exp-dummy-")
    
    # Check lifecycle calls
    mock_driver.install_prereqs.assert_called_once()
    mock_k8s.create_namespace.assert_called_once()
    mock_driver.deploy.assert_called_once()
    mock_driver.wait_ready.assert_called_once()
    mock_k8s.delete_namespace.assert_called_once()

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_with_load_and_faults(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    dummy_spec.load = LoadSpec(phases=[{"duration": "1s", "rate": "10/s"}])
    dummy_spec.faults = [FaultSpec(at="0s", worker="pod_kill", target={"pod": "test"})]
    
    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    
    mock_driver_cls = MagicMock()
    mock_get_driver.return_value = mock_driver_cls
    mock_driver = mock_driver_cls.return_value
    mock_driver.wait_load_started.return_value = time.time()
    
    with patch("k8ostester.core.runner.get_worker") as mock_get_worker:
        mock_worker_cls = MagicMock()
        mock_get_worker.return_value = mock_worker_cls
        mock_worker = mock_worker_cls.return_value
        
        runner = Runner(dummy_spec, results_root=tmp_path)
        runner.run()
        
        mock_driver.start_load.assert_called_once()
        mock_get_worker.assert_called_with("pod_kill")
        mock_worker.execute.assert_called_once()

@patch("k8ostester.core.runner.ClusterClient")
def test_runner_concurrent_run_error(mock_k8s_cls, dummy_spec, tmp_path):
    mock_k8s = mock_k8s_cls.return_value
    ns = MagicMock()
    ns.metadata.name = "other-run"
    mock_k8s.core.list_namespace.return_value.items = [ns]
    
    runner = Runner(dummy_spec, results_root=tmp_path)
    with pytest.raises(Exception, match="another experiment already occupies this cluster"):
        runner.run()

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_teardown_failure(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_k8s.delete_namespace.side_effect = Exception("delete failed")
    
    runner = Runner(dummy_spec, results_root=tmp_path)
    res = runner.run()
    
    assert res.status == "error"
    assert "delete failed" in res.error

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_keep_namespace(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    
    runner = Runner(dummy_spec, results_root=tmp_path, keep=True)
    runner.run()
    
    mock_k8s.delete_namespace.assert_not_called()

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_verify_and_goals(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    dummy_spec.verify = ["integrity", {"pitr": {"anchor": "pause"}}]
    dummy_spec.goals = [GoalSpec(metric="availability", min="50%")]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_driver = mock_get_driver.return_value.return_value
    mock_driver.verify.return_value = {"check": "integrity", "passed": True, "detail": "all good"}
    mock_driver.op_records = [{"op": "write", "ok": True, "t": 1.0}]

    res = Runner(dummy_spec, results_root=tmp_path).run()

    assert res.status == "passed"
    mock_driver.verify.assert_any_call("integrity", {})
    mock_driver.verify.assert_any_call("pitr", {"anchor": "pause"})  # config dict form
    assert res.goals[0]["passed"] is True

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_failed_verification_fails_run(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    dummy_spec.verify = ["integrity"]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_driver = mock_get_driver.return_value.return_value
    mock_driver.verify.return_value = {"check": "integrity", "passed": False, "detail": "2 writes lost"}

    res = Runner(dummy_spec, results_root=tmp_path).run()
    assert res.status == "failed"

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_backup_taken_before_load(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    dummy_spec.verify = ["backup"]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_driver = mock_get_driver.return_value.return_value
    mock_driver.verify.return_value = {"check": "backup", "passed": True, "detail": "ok"}

    Runner(dummy_spec, results_root=tmp_path).run()
    mock_driver.ensure_backup.assert_called_once()

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_faults_require_load(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    dummy_spec.faults = [FaultSpec(at="0s", worker="pod_kill", target={"pod": "x"})]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []

    with pytest.raises(K8osConfigError, match="faults require a load plan"):
        Runner(dummy_spec, results_root=tmp_path).run()

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_fault_cleanup_failure_is_logged_not_fatal(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path, fake_clock):
    dummy_spec.load = LoadSpec(phases=[{"duration": "1s", "rate": "10/s"}])
    # at 1s from load start: the runner sleeps up to the fault offset
    dummy_spec.faults = [FaultSpec(at="1s", worker="pod_kill", target={"pod": "x"})]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_driver = mock_get_driver.return_value.return_value
    mock_driver.wait_load_started.return_value = time.time()

    def bad_cleanup():
        raise RuntimeError("uncordon failed")

    with patch("k8ostester.core.runner.get_worker") as mock_get_worker:
        mock_get_worker.return_value.return_value.execute.return_value = bad_cleanup
        res = Runner(dummy_spec, results_root=tmp_path).run()

    assert res.status == "passed"  # cleanup failure must not fail the verdict
    events = EventLog.read(res.run_dir / "events.jsonl")
    assert any(e["type"] == "teardown.error" for e in events)

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_warns_single_node_for_node_faults(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path):
    dummy_spec.faults = [FaultSpec(at="0s", worker="node_drain", target={"node_of": "primary"})]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_probe.return_value.multi_node = False
    mock_probe.return_value.worker_count = 1

    runner_obj = Runner(dummy_spec, results_root=tmp_path)
    with pytest.raises(K8osConfigError):  # faults still need a load plan
        runner_obj.run()

    events = EventLog.read(runner_obj.run_dir / "events.jsonl")
    assert any(e["type"] == "capability.warn" for e in events)

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_detects_host_suspend(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path, fake_clock):
    """Wall clock advancing past monotonic during the measurement window means
    the host slept — the verdict would be fiction, so the run errors out."""
    dummy_spec.load = LoadSpec(phases=[{"duration": "1s", "rate": "10/s"}])

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_driver = mock_get_driver.return_value.return_value
    mock_driver.wait_load_started.return_value = fake_clock.time()
    mock_driver.wait_load_done.side_effect = lambda: fake_clock.suspend(10)

    with pytest.raises(K8osInfraError, match="host slept"):
        Runner(dummy_spec, results_root=tmp_path).run()

def test_runner_error_handling(dummy_spec, tmp_path):
    with patch("k8ostester.core.runner.ClusterClient") as mock_k8s_cls, \
         patch("k8ostester.core.runner.get_driver") as mock_get_driver, \
         patch("k8ostester.core.runner.probe") as mock_probe:
        
        mock_k8s = mock_k8s_cls.return_value
        mock_k8s.core.list_namespace.return_value.items = []
        
        mock_driver_cls = MagicMock()
        mock_get_driver.return_value = mock_driver_cls
        mock_driver = mock_driver_cls.return_value
        
        # Fail at deploy
        mock_driver.deploy.side_effect = Exception("deploy failed")
        
        runner = Runner(dummy_spec, results_root=tmp_path)
        # Verify it raises when deploy fails (some parts of run() might not catch everything)
        with pytest.raises(Exception, match="deploy failed"):
            runner.run()
        
        # Ensure cleanup still happened
        mock_k8s.delete_namespace.assert_called_once()

@patch("k8ostester.core.runner.ClusterClient")
@patch("k8ostester.core.runner.get_driver")
@patch("k8ostester.core.runner.probe")
def test_runner_samples_telemetry_while_waiting_for_fault(mock_probe, mock_get_driver, mock_k8s_cls, dummy_spec, tmp_path, fake_clock):
    dummy_spec.load = LoadSpec(phases=[{"duration": "30s", "rate": "10/s"}])
    dummy_spec.faults = [FaultSpec(at="12s", worker="pod_kill", target={"pod": "x"})]

    mock_k8s = mock_k8s_cls.return_value
    mock_k8s.core.list_namespace.return_value.items = []
    mock_driver = mock_get_driver.return_value.return_value
    mock_driver.wait_load_started.return_value = time.time()

    with patch("k8ostester.core.runner.get_worker"):
        Runner(dummy_spec, results_root=tmp_path).run()

    # 12s offset in <=5s slices: telemetry sampled on each slice (5+5+2)
    assert mock_driver.emit_live_telemetry.call_count == 3
