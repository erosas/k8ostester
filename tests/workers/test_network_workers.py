import pytest
import json
from unittest.mock import MagicMock, patch, ANY
from kubernetes import client
from k8ostester.workers.network import NetworkPartitionWorker, NetworkLossWorker, NetworkDelayWorker
from k8ostester.core.experiment import FaultSpec

@pytest.fixture
def mock_context():
    k8s = MagicMock()
    driver = MagicMock()
    events = MagicMock()
    return k8s, driver, events, "test-ns"

@patch("k8ostester.workers.network.load_resource")
def test_network_partition_worker(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")
    
    k8s.has_crd.return_value = True
    mock_load.return_value = {"metadata": {"name": "dummy"}}
    
    cleanup = worker.execute(fault)
    
    k8s.custom.create_namespaced_custom_object.assert_called_once()
    args = k8s.custom.create_namespaced_custom_object.call_args[0]
    assert args[0] == "chaos-mesh.org"
    assert args[2] == ns
    assert args[4] == {"metadata": {"name": "dummy"}}
    
    events.emit.assert_any_call(
        "fault.network_partition",
        "network_partition on my-pod for 30s",
        pod="my-pod", duration="30s", chaos=ANY
    )
    
    # Test cleanup
    cleanup()
    k8s.custom.delete_namespaced_custom_object.assert_called_once()
    events.emit.assert_any_call("fault.cleanup", ANY)

def test_network_chaos_missing_crd(mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")
    
    k8s.has_crd.return_value = False
    with pytest.raises(RuntimeError, match="needs Chaos Mesh"):
        worker.execute(fault)

def test_network_chaos_missing_duration(mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}) # No duration
    
    k8s.has_crd.return_value = True
    with pytest.raises(ValueError, match="needs a 'duration'"):
        worker.execute(fault)

@patch("k8ostester.workers.network.load_resource")
def test_network_loss_worker(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkLossWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_loss", target={"pod": "my-pod"}, duration="30s", params={"loss": "20"})
    
    k8s.has_crd.return_value = True
    mock_load.return_value = {}
    
    worker.execute(fault)
    
    # Check that extra variables were passed to load_resource
    variables = mock_load.call_args[0][1]
    assert variables["LOSS"] == "20"

@patch("k8ostester.workers.network.load_resource")
def test_network_delay_worker(mock_load, mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkDelayWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_delay", target={"pod": "my-pod"}, duration="30s", params={"latency": "200ms", "jitter": "10ms"})
    
    k8s.has_crd.return_value = True
    mock_load.return_value = {}
    
    worker.execute(fault)
    
    variables = mock_load.call_args[0][1]
    assert variables["LATENCY"] == "200ms"
    assert variables["JITTER"] == "10ms"

def test_network_chaos_cleanup_404(mock_context):
    k8s, driver, events, ns = mock_context
    worker = NetworkPartitionWorker(k8s, driver, ns, events)
    fault = FaultSpec(at="1s", worker="network_partition", target={"pod": "my-pod"}, duration="30s")
    
    k8s.has_crd.return_value = True
    with patch("k8ostester.workers.network.load_resource", return_value={}):
        cleanup = worker.execute(fault)
    
    # Mock ApiException with 404
    api_exc = client.ApiException(status=404)
    k8s.custom.delete_namespaced_custom_object.side_effect = api_exc
    
    # Should not raise
    cleanup()
