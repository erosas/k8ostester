"""Discover a CNPG cluster into the state snapshot the console renders from.

The snapshot feeds both the capability map (``pg.control``) and the UI. ``snapshot``
does the cluster I/O; ``build_snapshot`` is the pure transform (unit-tested).
See docs/remote-control.md.
"""
from __future__ import annotations

from k8ostester_kernel import chaos
from k8ostester_kernel.k8s import ClusterClient

from k8ostester_pg import harness

CNPG_GROUP, CNPG_VERSION = "postgresql.cnpg.io", "v1"


def pg_version(image: str) -> str:
    """'ghcr.io/.../postgresql:16.4' -> '16.4' (tag after the last colon)."""
    return image.rsplit(":", 1)[-1] if ":" in image else ""


def build_snapshot(
    cluster: dict,
    replica_pods: list[str],
    zones: list[str],
    backups: list[dict],
    partitioned: bool,
    target: str = "",
) -> dict:
    """Pure transform: CNPG objects -> the flat snapshot the actions read."""
    spec = cluster.get("spec", {})
    status = cluster.get("status", {})
    instances = spec.get("instances", 0)
    ready_n = int(status.get("readyInstances", 0) or 0)
    managed = [r.get("name") for r in spec.get("managed", {}).get("roles", [])]
    completed = sum(
        1 for b in backups if b.get("status", {}).get("phase") == "completed"
    )
    phase = str(status.get("phase", "")).lower()
    return {
        "ready": instances > 0 and ready_n == instances,
        "primary": status.get("currentPrimary", ""),
        "replicas": replica_pods,
        "zones": zones,
        "version": pg_version(spec.get("imageName", "")),
        "target": pg_version(target) if target else "",
        "upgrading": "upgrad" in phase,
        "backup_configured": "backup" in spec,
        "backups_completed": completed,
        "pitr_window": completed > 0,   # a completed base backup opens the window
        "blue_green": "app_a" in managed and "app_b" in managed,
        "fault_in_flight": partitioned,
    }


def snapshot(k8s: ClusterClient, namespace: str, name: str = "pg",
             target: str = "") -> dict:
    """Read the live cluster and produce its snapshot."""
    cluster = k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters", name)
    replica_pods = harness.replicas(k8s, namespace)
    # zones of the instance pods' nodes
    zones = sorted({
        z for z in (
            _node_zone(k8s, p.spec.node_name)
            for p in k8s.core.list_namespaced_pod(
                namespace, label_selector=f"cnpg.io/cluster={name}").items
            if p.spec.node_name
        ) if z
    })
    backups = k8s.custom.list_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "backups").get("items", [])
    partitioned = _partition_active(k8s, namespace)
    return build_snapshot(cluster, replica_pods, zones, backups, partitioned, target)


def _node_zone(k8s: ClusterClient, node: str) -> str:
    labels = k8s.core.read_node(node).metadata.labels or {}
    return labels.get("topology.kubernetes.io/zone", "")


def _partition_active(k8s: ClusterClient, namespace: str) -> bool:
    from kubernetes import client
    try:
        k8s.networking.read_namespaced_network_policy(chaos.PARTITION_POLICY, namespace)
        return True
    except client.ApiException as e:
        if e.status == 404:
            return False
        raise
