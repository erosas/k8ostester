"""Unit tests for the pg harness helpers — mock the k8s client, no cluster."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from k8ostester_pg import harness


def test_cluster_field_reads_status():
    k8s = MagicMock()
    k8s.custom.get_namespaced_custom_object.return_value = {
        "status": {"currentPrimary": "pg-1", "readyInstances": 3}
    }
    assert harness.cluster_field(k8s, "ns", "currentPrimary") == "pg-1"
    assert harness.cluster_field(k8s, "ns", "readyInstances") == "3"
    # missing field → empty string (callers compare against it)
    assert harness.cluster_field(k8s, "ns", "nope") == ""


def test_replicas_lists_replica_pods_only():
    k8s = MagicMock()
    k8s.core.list_namespaced_pod.return_value = SimpleNamespace(items=[
        SimpleNamespace(metadata=SimpleNamespace(name="pg-2")),
        SimpleNamespace(metadata=SimpleNamespace(name="pg-3")),
    ])
    assert harness.replicas(k8s, "ns") == ["pg-2", "pg-3"]
    # queried by the replica role label, not the primary
    assert "cnpg.io/instanceRole=replica" in \
        k8s.core.list_namespaced_pod.call_args.kwargs["label_selector"]


def test_print_verdict_exit_codes(capsys):
    passing = {"experiment": "x", "verdict": "pass", "slo": {}, "verifies": {"rpo": True}}
    failing = {"experiment": "x", "verdict": "fail",
               "slo": {"error_rate": {"pass": False, "observed": 1.0,
                                      "threshold": 0.01, "direction": "max"}},
               "verifies": {"rpo": True}}
    assert harness.print_verdict(passing) == 0
    assert harness.print_verdict(failing) == 1
    out = capsys.readouterr().out
    assert "PASS" in out and "FAIL" in out
