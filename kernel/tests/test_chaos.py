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


def test_partition_labels_the_pod_and_applies_a_deny_all_policy():
    k8s = MagicMock()
    chaos.partition_pod(k8s, "ns", "pg-1")
    # the pod is labelled with the partition label = its own name
    patch = k8s.core.patch_namespaced_pod.call_args.args
    assert patch[0] == "pg-1" and patch[2]["metadata"]["labels"][chaos.PARTITION_LABEL] == "pg-1"
    # a NetworkPolicy denies all ingress+egress for that label (no rules)
    ns, body = k8s.networking.create_namespaced_network_policy.call_args.args
    assert ns == "ns"
    assert body["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert body["spec"]["podSelector"]["matchLabels"][chaos.PARTITION_LABEL] == "pg-1"
    assert "ingress" not in body["spec"] and "egress" not in body["spec"]


def test_heal_partition_removes_policy_and_label():
    k8s = MagicMock()
    chaos.heal_partition(k8s, "ns", "pg-1")
    assert k8s.networking.delete_namespaced_network_policy.call_args.args == (chaos.PARTITION_POLICY, "ns")
    # label cleared (None removes it)
    assert k8s.core.patch_namespaced_pod.call_args.args[2]["metadata"]["labels"][chaos.PARTITION_LABEL] is None
