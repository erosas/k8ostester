"""Chaos primitives — raw fault operations on a ``ClusterClient``.

Plain functions, deliberately *not* wrapped in a Worker/FaultSpec abstraction
(that lives in the old experiment engine on its way out). Verticals call these
directly. Network partition (NetworkPolicy) is intentionally not here yet — it
carries more state (labels + a heal timer) and wants live validation before it
moves; it stays in the vertical until then.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kubernetes import client

if TYPE_CHECKING:
    from k8ostester_kernel.k8s import ClusterClient


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
