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

@patch("k8ostester.core.infra.Helm")
def test_ensure_chaos_mesh_tolerates_repo_blip(mock_helm_cls, mock_context):
    from k8ostester.core.helm import HelmError
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)
    mock_helm = mock_helm_cls.return_value
    mock_helm.repo_add.side_effect = HelmError("repo unreachable")

    # already installed: the blip is tolerated
    mock_helm.release_exists.return_value = True
    mgr.ensure(["chaos-mesh"])

    # not installed: the blip is fatal
    mock_helm.release_exists.return_value = False
    with pytest.raises(HelmError):
        mgr.ensure(["chaos-mesh"])

def test_ensure_seaweedfs_creates_namespace(mock_context):
    from kubernetes import client
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)

    k8s.core.read_namespace.side_effect = client.ApiException(status=404)
    pod = MagicMock()
    pod.metadata.name = "weed-0"
    pod.status.phase = "Running"
    k8s.core.list_namespaced_pod.return_value.items = [pod]
    k8s.exec_pod.return_value = "Bucket created"

    mgr.ensure(["seaweedfs"])
    k8s.create_namespace.assert_called_once()

def test_ensure_seaweedfs_bucket_failure(mock_context):
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)

    k8s.core.read_namespace.return_value = True
    pod = MagicMock()
    pod.metadata.name = "weed-0"
    pod.status.phase = "Running"
    k8s.core.list_namespaced_pod.return_value.items = [pod]
    k8s.exec_pod.return_value = "error: cannot reach filer"

    with pytest.raises(K8osInfraError, match="bucket creation failed"):
        mgr.ensure(["seaweedfs"])

def test_ensure_seaweedfs_existing_bucket_ok(mock_context):
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)

    k8s.core.read_namespace.return_value = True
    pod = MagicMock()
    pod.metadata.name = "weed-0"
    pod.status.phase = "Running"
    k8s.core.list_namespaced_pod.return_value.items = [pod]
    k8s.exec_pod.return_value = "error: bucket already exists"

    mgr.ensure(["seaweedfs"])  # idempotent: an existing bucket is fine

def test_ensure_namespace_other_error_raises(mock_context):
    from kubernetes import client
    k8s, events = mock_context
    mgr = InfraManager(k8s, events)
    k8s.core.read_namespace.side_effect = client.ApiException(status=403)
    with pytest.raises(client.ApiException):
        mgr._ensure_namespace("k8ost-infra")
