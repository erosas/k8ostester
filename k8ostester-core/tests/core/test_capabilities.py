from unittest.mock import MagicMock, patch

from k8ostester.core.capabilities import (
    Capabilities,
    NodeInfo,
    _helm_version,
    _kubectl_version,
    probe,
)


def test_capabilities_logic():
    nodes = [
        NodeInfo(name="n1", roles=["control-plane"], ready=True, arch="arm64", kubelet_version="v1.31"),
        NodeInfo(name="n2", roles=["worker"], ready=True, arch="arm64", kubelet_version="v1.31"),
        NodeInfo(name="n3", roles=["worker"], ready=True, arch="arm64", kubelet_version="v1.31"),
    ]
    caps = Capabilities(
        context="test", server_version="v1.31", nodes=nodes, storage_classes=[],
        snapshot_crds=True, snapshot_classes=["snap1"], operators={},
        helm_version="v3.15", kubectl_version="v1.31"
    )
    assert caps.worker_count == 2
    assert caps.multi_node is True
    assert caps.snapshots_supported is True

    caps.nodes = nodes[:2] # only 1 worker
    assert caps.worker_count == 1
    assert caps.multi_node is False

@patch("shutil.which")
@patch("subprocess.run")
def test_version_probing(mock_run, mock_which):
    mock_which.return_value = "/usr/local/bin/kubectl"
    mock_run.return_value = MagicMock(returncode=0, stdout="Client Version: v1.31.1\n")
    assert _kubectl_version() == "v1.31.1"
    
    # Test fallback if --short fails
    mock_run.side_effect = [
        MagicMock(returncode=1, stderr="error: unknown flag --short"),
        MagicMock(returncode=0, stdout="Client Version: v1.32.0\n")
    ]
    assert _kubectl_version() == "v1.32.0"

    mock_run.side_effect = None
    mock_run.return_value = MagicMock(returncode=0, stdout="v3.15.0\n")
    assert _helm_version() == "v3.15.0"

    # Test missing binaries
    mock_which.return_value = None
    assert _kubectl_version() is None
    assert _helm_version() is None

@patch("k8ostester.core.capabilities.ClusterClient")
def test_probe_and_node_info(mock_client_cls):
    mock_k8s = mock_client_cls.return_value
    mock_k8s.version.get_code.return_value = MagicMock(git_version="v1.31.0")
    
    # Mock node
    node = MagicMock()
    node.metadata.name = "node-1"
    node.metadata.labels = {"node-role.kubernetes.io/worker": ""}
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True"
    node.status.conditions = [cond]
    node.status.node_info.architecture = "amd64"
    node.status.node_info.kubelet_version = "v1.31.0"
    mock_k8s.core.list_node.return_value.items = [node]
    
    # Mock SC
    sc = MagicMock()
    sc.metadata.name = "standard"
    sc.provisioner = "k8s.io/minikube-hostpath"
    sc.metadata.annotations = {"storageclass.kubernetes.io/is-default-class": "true"}
    mock_k8s.storage.list_storage_class.return_value.items = [sc]
    
    mock_k8s.has_crd.return_value = True  # snapshot CRD present → classes probed
    mock_k8s.custom.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"name": "snapclass-1"}}]
    }

    with patch("k8ostester.core.capabilities._helm_version", return_value="v3.15.0"), \
         patch("k8ostester.core.capabilities._kubectl_version", return_value="v1.31.0"):
        caps = probe("my-ctx")
        assert caps.context == "my-ctx"
        assert caps.server_version == "v1.31.0"
        assert len(caps.nodes) == 1
        assert caps.nodes[0].name == "node-1"
        assert caps.storage_classes[0].is_default is True
        assert caps.snapshot_classes == ["snapclass-1"]

@patch("k8ostester.core.capabilities.ClusterClient")
def test_probe_snapshot_classes_absent(mock_client_cls):
    mock_k8s = mock_client_cls.return_value
    mock_k8s.version.get_code.return_value = MagicMock(git_version="v1.31.0")
    mock_k8s.core.list_node.return_value.items = []
    mock_k8s.storage.list_storage_class.return_value.items = []
    mock_k8s.has_crd.return_value = True
    # CRD present but listing the classes fails → treated as none installed
    mock_k8s.custom.list_cluster_custom_object.side_effect = Exception("404")

    with patch("k8ostester.core.capabilities._helm_version", return_value=None), \
         patch("k8ostester.core.capabilities._kubectl_version", return_value=None):
        caps = probe(None)
        assert caps.snapshot_classes == []
        assert caps.snapshots_supported is False

@patch("shutil.which", return_value="/usr/local/bin/kubectl")
@patch("subprocess.run")
def test_kubectl_version_both_forms_fail(mock_run, mock_which):
    mock_run.return_value = MagicMock(returncode=1, stderr="broken")
    assert _kubectl_version() is None


@patch("k8ostester.core.capabilities.ClusterClient")
def test_probe_reports_zones(mock_client_cls):
    mock_k8s = mock_client_cls.return_value
    mock_k8s.version.get_code.return_value = MagicMock(git_version="v1.31.0")
    node = MagicMock()
    node.metadata.name = "ip-10-0-1-5"
    node.metadata.labels = {"topology.kubernetes.io/zone": "us-east-1a"}
    node.status.conditions = []
    node.status.node_info.architecture = "amd64"
    node.status.node_info.kubelet_version = "v1.31.0"
    mock_k8s.core.list_node.return_value.items = [node]
    mock_k8s.storage.list_storage_class.return_value.items = []
    mock_k8s.has_crd.return_value = False

    with patch("k8ostester.core.capabilities._helm_version", return_value=None), \
         patch("k8ostester.core.capabilities._kubectl_version", return_value=None):
        caps = probe(None)
    assert caps.nodes[0].zone == "us-east-1a"
