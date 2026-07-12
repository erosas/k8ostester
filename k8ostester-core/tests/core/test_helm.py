from pathlib import Path
from unittest.mock import patch

import pytest

from k8ostester.core.helm import Helm, HelmError


@pytest.fixture
def mock_k8s():
    with patch("k8ostester.core.k8s.ClusterClient") as mock:
        mock.return_value._check_helm.return_value = "/usr/bin/helm"
        yield mock

def test_helm_repo_add(mock_k8s):
    helm = Helm()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        helm.repo_add("my-repo", "http://charts")
        assert mock_run.call_count == 2 # repo add, repo update

def test_helm_upgrade_install(mock_k8s):
    helm = Helm(context="test-ctx")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        helm.upgrade_install(
            "my-release", "my-chart", "my-ns",
            version="1.2.3", set_values={"foo": "bar"},
            values_file=Path("/tmp/values.yaml"),
        )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "upgrade" in cmd
        assert "--install" in cmd
        assert "my-release" in cmd
        assert "--namespace" in cmd
        assert "my-ns" in cmd
        assert "--version" in cmd
        assert "1.2.3" in cmd
        assert "--values" in cmd
        assert "/tmp/values.yaml" in cmd
        assert "--set" in cmd
        assert "foo=bar" in cmd
        assert "--kube-context" in cmd
        assert "test-ctx" in cmd

def test_helm_error(mock_k8s):
    helm = Helm()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "boom"
        with pytest.raises(HelmError, match="failed"):
            helm.uninstall("release", "ns")

def test_helm_release_exists(mock_k8s):
    helm = Helm()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "rel1\nrel2\n"
        
        assert helm.release_exists("rel1", "ns") is True
        assert helm.release_exists("rel3", "ns") is False
