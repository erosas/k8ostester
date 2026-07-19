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


def test_kill_primary_calls_the_kernel_primitive():
    k8s = MagicMock()
    msg = execute(k8s, "ns", "kill-primary", snap())
    assert k8s.core.delete_namespaced_pod.call_args.args[0] == "pg-1"
    assert "pg-1" in msg


def test_partition_and_kill_replica_wired():
    k8s = MagicMock()
    execute(k8s, "ns", "partition-primary", snap())
    k8s.networking.create_namespaced_network_policy.assert_called_once()
    execute(k8s, "ns", "kill-replica", snap())
    assert k8s.core.delete_namespaced_pod.call_args.args[0] == "pg-2"


def test_backup_creates_a_backup_cr():
    k8s = MagicMock()
    execute(k8s, "ns", "backup", snap())
    _, _, ns, plural, body = k8s.custom.create_namespaced_custom_object.call_args.args
    assert plural == "backups" and body["kind"] == "Backup"


def test_execute_gates_on_the_capability():
    # a fault in flight disables kill-primary → execute refuses, no primitive fires
    k8s = MagicMock()
    with pytest.raises(ActionDenied):
        execute(k8s, "ns", "kill-primary", snap(fault_in_flight=True))
    k8s.core.delete_namespaced_pod.assert_not_called()


@patch("k8ostester_pg.execute.ops")
def test_ops_actions_dispatch_to_the_ops_module(mock_ops):
    k8s = MagicMock()
    execute(k8s, "ns", "upgrade", snap())
    mock_ops.minor_upgrade.assert_called_once_with(k8s, "ns", "16.6")   # the target
    execute(k8s, "ns", "rotate", snap())
    mock_ops.rotate_credentials.assert_called_once_with(k8s, "ns")
    execute(k8s, "ns", "restore", snap())
    mock_ops.restore.assert_called_once_with(k8s, "ns", "")
