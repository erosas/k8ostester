"""Unit tests for the chaos primitives — mock the k8s client, no cluster."""
from unittest.mock import MagicMock

from k8ostester_kernel import chaos


def test_kill_pod_deletes_with_grace_zero_by_default():
    k8s = MagicMock()
    chaos.kill_pod(k8s, "ns", "pod-1")
    (name, namespace), kwargs = k8s.core.delete_namespaced_pod.call_args
    assert name == "pod-1"
    assert namespace == "ns"
    assert kwargs["body"].grace_period_seconds == 0


def test_kill_pod_honors_grace_period():
    k8s = MagicMock()
    chaos.kill_pod(k8s, "ns", "pod-1", grace_period=30)
    assert k8s.core.delete_namespaced_pod.call_args.kwargs["body"].grace_period_seconds == 30


def test_cordon_and_uncordon_toggle_unschedulable():
    k8s = MagicMock()
    chaos.cordon_node(k8s, "node-a")
    assert k8s.core.patch_node.call_args.args == ("node-a", {"spec": {"unschedulable": True}})
    chaos.uncordon_node(k8s, "node-a")
    assert k8s.core.patch_node.call_args.args == ("node-a", {"spec": {"unschedulable": False}})
