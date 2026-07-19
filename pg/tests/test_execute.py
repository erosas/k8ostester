"""Unit tests for the action executor — mock the k8s client, no cluster."""
from unittest.mock import MagicMock, patch

import pytest
from k8ostester_pg.execute import ActionDenied, execute


def snap(**over):
    base = dict(ready=True, primary="pg-1", replicas=["pg-2", "pg-3"],
                zones=["a", "b", "c"], version="16.4", target="16.6",
                upgrading=False, backup_configured=True, backups_completed=1,
                pitr_window=True, blue_green=True, fault_in_flight=False)
    return {**base, **over}


def test_kill_pod_defaults_to_primary_and_honours_a_target():
    k8s = MagicMock()
    execute(k8s, "ns", "kill-pod", snap())
    assert k8s.core.delete_namespaced_pod.call_args.args[0] == "pg-1"   # default = primary
    execute(k8s, "ns", "kill-pod", snap(), params={"pod": "pg-3"})
    assert k8s.core.delete_namespaced_pod.call_args.args[0] == "pg-3"   # explicit replica
    execute(k8s, "ns", "kill-pod", snap(), params={"pod": "nope"})
    assert k8s.core.delete_namespaced_pod.call_args.args[0] == "pg-1"   # unknown -> primary


def test_partition_pod_targets_any_instance():
    k8s = MagicMock()
    execute(k8s, "ns", "partition-pod", snap(), params={"pod": "pg-2"})
    k8s.networking.create_namespaced_network_policy.assert_called_once()


def test_backup_creates_a_backup_cr():
    k8s = MagicMock()
    execute(k8s, "ns", "backup", snap())
    _, _, ns, plural, body = k8s.custom.create_namespaced_custom_object.call_args.args
    assert plural == "backups" and body["kind"] == "Backup"


def test_execute_gates_on_the_capability():
    # a fault in flight disables kill-pod → execute refuses, no primitive fires
    k8s = MagicMock()
    with pytest.raises(ActionDenied):
        execute(k8s, "ns", "kill-pod", snap(fault_in_flight=True))
    k8s.core.delete_namespaced_pod.assert_not_called()


@patch("k8ostester_pg.execute.ops")
def test_ops_actions_dispatch_to_the_ops_module(mock_ops):
    k8s = MagicMock()
    # target comes from the modal (params); falls back to the snapshot's suggestion
    execute(k8s, "ns", "upgrade", snap(), name="orders", params={"target": "16.8"})
    mock_ops.minor_upgrade.assert_called_once_with(k8s, "ns", "16.8", "orders")
    execute(k8s, "ns", "rotate", snap(), name="orders", params={"password": "s3cr3t"})
    mock_ops.rotate_credentials.assert_called_once_with(k8s, "ns", "orders", "s3cr3t")
    execute(k8s, "ns", "restore", snap(), name="orders")
    mock_ops.restore.assert_called_once_with(k8s, "ns", "", "orders")


def test_backup_uses_the_selected_cluster_name():
    k8s = MagicMock()
    execute(k8s, "ns", "backup", snap(), name="orders")
    body = k8s.custom.create_namespaced_custom_object.call_args.args[-1]
    assert body["spec"]["cluster"]["name"] == "orders"
