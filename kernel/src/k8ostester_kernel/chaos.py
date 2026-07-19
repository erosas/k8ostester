"""Chaos primitives — raw fault operations on a ``ClusterClient``.

Plain functions, deliberately *not* wrapped in a Worker/FaultSpec abstraction
(that lived in the retired experiment engine). Verticals call these directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kubernetes import client

if TYPE_CHECKING:
    from k8ostester_kernel.k8s import ClusterClient

PARTITION_LABEL = "k8ostester.io/partition"
PARTITION_POLICY = "k8ost-partition"


def kill_pod(k8s: ClusterClient, namespace: str, name: str, grace_period: int = 0) -> None:
    """Delete a pod immediately (grace 0 by default) — models a hard crash."""
    k8s.core.delete_namespaced_pod(
        name, namespace, body=client.V1DeleteOptions(grace_period_seconds=grace_period)
    )


def cordon_node(k8s: ClusterClient, node: str) -> None:
    """Mark a node unschedulable — the compute side of a node/AZ drain."""
    k8s.core.patch_node(node, {"spec": {"unschedulable": True}})


def uncordon_node(k8s: ClusterClient, node: str) -> None:
    """Undo cordon_node — mark the node schedulable again."""
    k8s.core.patch_node(node, {"spec": {"unschedulable": False}})


def partition_pod(k8s: ClusterClient, namespace: str, pod: str) -> None:
    """Network-isolate a pod: label it, then apply a NetworkPolicy denying all
    ingress and egress for that label (empty policyTypes rules = deny). Native,
    no Chaos Mesh. Requires a CNI that ENFORCES NetworkPolicy (Calico/Cilium);
    on a non-enforcing CNI (kindnet/docker-desktop) it applies but has no effect.
    Undo with heal_partition.
    """
    k8s.core.patch_namespaced_pod(
        pod, namespace, {"metadata": {"labels": {PARTITION_LABEL: pod}}}
    )
    k8s.networking.create_namespaced_network_policy(namespace, {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": PARTITION_POLICY},
        "spec": {
            "podSelector": {"matchLabels": {PARTITION_LABEL: pod}},
            "policyTypes": ["Ingress", "Egress"],   # no rules → deny all
        },
    })


def heal_partition(k8s: ClusterClient, namespace: str, pod: str) -> None:
    """Remove the partition NetworkPolicy and the pod's partition label."""
    k8s.networking.delete_namespaced_network_policy(PARTITION_POLICY, namespace)
    k8s.core.patch_namespaced_pod(
        pod, namespace, {"metadata": {"labels": {PARTITION_LABEL: None}}}
    )
