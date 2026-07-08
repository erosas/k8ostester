import pytest
from unittest.mock import MagicMock, patch, ANY
from k8ostester.core.infra import InfraManager
from k8ostester.core.exceptions import K8osConfigError, K8osInfraError

@pytest.fixture
def mock_context():
    k8s = MagicMock()
    events = MagicMock()
    return k8s, events

def test_infra_handles(mock_context):
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)
    assert mgr.handles("seaweedfs") is True
    assert mgr.handles("chaos-mesh") is True
    assert mgr.handles("invalid") is False

@patch("k8ostester.core.infra.Helm")
def test_ensure_chaos_mesh(mock_helm_cls, mock_context):
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)
    
    mock_helm = mock_helm_cls.return_value
    mgr.ensure(["chaos-mesh"])
    
    mock_helm.repo_add.assert_called_with("chaos-mesh", ANY)
    mock_helm.upgrade_install.assert_called()
    events.emit.assert_any_call("infra.chaos-mesh", ANY)

def test_ensure_seaweedfs(mock_context):
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)
    
    # Mock namespace check (already exists)
    k8s.core.read_namespace.return_value = True
    
    # Mock pods for bucket creation
    pod = MagicMock()
    pod.metadata.name = "weed-0"
    pod.status.phase = "Running"
    k8s.core.list_namespaced_pod.return_value.items = [pod]
    
    k8s.exec_pod.return_value = "Bucket created"
    
    mgr.ensure(["seaweedfs"])
    
    k8s.apply_manifests.assert_called()
    k8s.wait_workloads_ready.assert_called()
    k8s.exec_pod.assert_called()
    events.emit.assert_any_call("infra.seaweedfs", ANY)

def test_ensure_invalid(mock_context):
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)
    with pytest.raises(K8osConfigError, match="not common infra"):
        mgr.ensure(["invalid"])
