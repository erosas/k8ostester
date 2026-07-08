import pytest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY
from k8ostester.core.runner import Runner, RunResult
from k8ostester.core.experiment import ExperimentSpec, LoadSpec, FaultSpec, ClusterSpec

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
