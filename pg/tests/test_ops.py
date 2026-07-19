"""Unit tests for the multi-step CNPG ops — mock the k8s client, no cluster."""
from unittest.mock import MagicMock

from k8ostester_pg import ops


def cluster_obj():
    return {
        "spec": {"imageName": "ghcr.io/cloudnative-pg/postgresql:16.4",
                 "storage": {"size": "1Gi"},
                 "backup": {"barmanObjectStore": {"destinationPath": "s3://backups/x"}}},
        "status": {"currentPrimary": "pg-1"},
    }


def test_minor_upgrade_swaps_only_the_tag():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    ops.minor_upgrade(k8s, "ns", "16.6")
    patch = k8s.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert patch["spec"]["imageName"] == "ghcr.io/cloudnative-pg/postgresql:16.6"


def test_rotate_alters_idle_role_flips_selector_and_rolls():
    k8s = MagicMock()
    k8s.core.read_namespaced_config_map.return_value.data = {"active": "a"}
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    msg = ops.rotate_credentials(k8s, "ns")
    # ALTER ROLE on the IDLE role (b), through the primary
    exec_cmd = k8s.exec_pod.call_args.args
    assert exec_cmd[1] == "pg-1" and "alter role app_b password" in exec_cmd[2][-1]
    # selector flipped to the idle role, app rolled
    assert k8s.core.patch_namespaced_config_map.call_args.args[2] == {"data": {"active": "b"}}
    k8s.apps.patch_namespaced_deployment.assert_called_once()
    assert "app_a → app_b" in msg


def test_restore_creates_a_uniquely_named_recovery_cluster():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = cluster_obj()
    ops.restore(k8s, "ns")
    body = k8s.custom.create_namespaced_custom_object.call_args.args[-1]
    assert body["kind"] == "Cluster" and body["metadata"]["name"].startswith("pg-restore-")
    # recover to latest (no recoveryTarget) from the source's object store
    assert body["spec"]["bootstrap"]["recovery"] == {"source": "origin"}
    assert body["spec"]["externalClusters"][0]["barmanObjectStore"]["serverName"] == "pg"
