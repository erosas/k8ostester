import pytest
from unittest.mock import MagicMock, patch
from kubernetes import client
from k8ostester.core.k8s import ClusterClient, available_contexts
from k8ostester.core.exceptions import K8osInfraError

@pytest.fixture
def mock_config():
    with patch("k8ostester.core.k8s.config.new_client_from_config") as mock:
        yield mock

def test_k8s_has_crd(mock_config):
    k8s = ClusterClient()
    mock_apiext = MagicMock()
    # Need to mock the cached_property by patching it or just assigning to the instance
    with patch.object(ClusterClient, "apiext", mock_apiext):
        # Case 1: Exists
        mock_apiext.read_custom_resource_definition.return_value = True
        assert k8s.has_crd("my-crd") is True
        
        # Case 2: 404
        mock_apiext.read_custom_resource_definition.side_effect = client.ApiException(status=404)
        assert k8s.has_crd("missing") is False

def test_k8s_create_namespace(mock_config):
    k8s = ClusterClient()
    mock_core = MagicMock()
    with patch.object(ClusterClient, "core", mock_core):
        k8s.create_namespace("test-ns", labels={"foo": "bar"})
        mock_core.create_namespace.assert_called_once()
        body = mock_core.create_namespace.call_args[0][0]
        assert body.metadata.name == "test-ns"
        assert body.metadata.labels == {"foo": "bar"}

def test_k8s_delete_namespace_wait(mock_config):
    k8s = ClusterClient()
    mock_core = MagicMock()
    with patch.object(ClusterClient, "core", mock_core):
        # First call delete succeeds
        # Then read_namespace returns until it eventually returns 404
        mock_core.read_namespace.side_effect = [
            MagicMock(), # Still there
            client.ApiException(status=404) # Gone
        ]
        
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3]):
            k8s.delete_namespace("test-ns", wait=True)
            assert mock_core.delete_namespace.call_count == 1
            assert mock_core.read_namespace.call_count == 2

def test_k8s_apply_manifests_with_vars(mock_config, tmp_path):
    k8s = ClusterClient()
    f = tmp_path / "test.yaml"
    f.write_text("namespace: ${K8OST_NAMESPACE}")
    
    with patch("shutil.which", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "applied"
        
        res = k8s.apply_manifests(f, "ns-1", variables={"K8OST_NAMESPACE": "ns-1"})
        
        assert res == "applied"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "-n" in cmd
        assert "ns-1" in cmd
        # Verify substitution happened in stdin
        assert mock_run.call_args[1]["input"] == "namespace: ns-1"

def test_k8s_pod_logs(mock_config):
    k8s = ClusterClient()
    mock_core = MagicMock()
    with patch.object(ClusterClient, "core", mock_core):
        mock_resp = MagicMock()
        mock_resp.data = b"some logs"
        mock_core.read_namespaced_pod_log.return_value = mock_resp
        
        logs = k8s.pod_logs("ns", "pod")
        assert logs == "some logs"
        mock_core.read_namespaced_pod_log.assert_called_with(
            "pod", "ns", container=None, _preload_content=False
        )

def test_k8s_wait_workloads_ready(mock_config):
    k8s = ClusterClient()
    mock_apps = MagicMock()
    with patch.object(ClusterClient, "apps", mock_apps):
        # Mock a deployment that is initially not ready, then becomes ready
        d1 = MagicMock()
        d1.metadata.name = "d1"
        d1.spec.replicas = 1
        d1.status.ready_replicas = 0
        
        d1_ready = MagicMock()
        d1_ready.metadata.name = "d1"
        d1_ready.spec.replicas = 1
        d1_ready.status.ready_replicas = 1
        
        mock_apps.list_namespaced_deployment.side_effect = [
            MagicMock(items=[d1]),
            MagicMock(items=[d1_ready])
        ]
        mock_apps.list_namespaced_stateful_set.return_value = MagicMock(items=[])
        
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 1, 2, 3]):
            k8s.wait_workloads_ready("ns", timeout=10)
            assert mock_apps.list_namespaced_deployment.call_count == 2

def test_k8s_exec_pod_fail(mock_config):
    k8s = ClusterClient()
    with patch("shutil.which", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "exec failed"
        with pytest.raises(K8osInfraError, match="exec in pod-1 failed"):
            k8s.exec_pod("ns", "pod-1", ["ls"])

def test_k8s_delete_namespace_wait_timeout(mock_config):
    k8s = ClusterClient()
    mock_core = MagicMock()
    with patch.object(ClusterClient, "core", mock_core):
        mock_core.read_namespace.return_value = MagicMock()
        with patch("time.sleep"), patch("time.monotonic", side_effect=[0, 10, 1000]):
            with pytest.raises(TimeoutError, match="still terminating"):
                k8s.delete_namespace("test-ns", timeout=100)

def test_k8s_apply_manifests_fail(mock_config, tmp_path):
    k8s = ClusterClient()
    with patch("shutil.which", return_value="/usr/bin/kubectl"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "error apply"
        with pytest.raises(K8osInfraError, match="kubectl apply failed"):
            k8s.apply_manifests(tmp_path, "ns")

def test_available_contexts():
    with patch("k8ostester.core.k8s.config.list_kube_config_contexts") as mock_list:
        mock_list.return_value = (
            [{"name": "ctx1"}, {"name": "ctx2"}],
            {"name": "ctx1"}
        )
        names, active = available_contexts()
        assert names == ["ctx1", "ctx2"]
        assert active == "ctx1"

def test_k8s_delete_namespace_404(mock_config):
    k8s = ClusterClient()
    mock_core = MagicMock()
    with patch.object(ClusterClient, "core", mock_core):
        mock_core.delete_namespace.side_effect = client.ApiException(status=404)
        # Should not raise
        k8s.delete_namespace("missing-ns")
