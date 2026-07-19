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
        # target may be a full image (…:tag) or a bare version
        "target": (pg_version(target) if ":" in target else target) if target else "",
        "upgrading": "upgrad" in phase,
        "backup_configured": "backup" in spec,
        "backups_completed": completed,
        "pitr_window": completed > 0,   # a completed base backup opens the window
        "blue_green": "app_a" in managed and "app_b" in managed,
        "fault_in_flight": partitioned,
    }


def snapshot(k8s: ClusterClient, namespace: str, name: str = "pg",
             target: str = "") -> dict:
    """Read the live cluster and produce its snapshot (capability fields + the
    richer topology the UI renders)."""
    cluster = k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters", name)
    replica_pods = harness.replicas(k8s, namespace)
    instances = _instances(k8s, namespace, name)
    zones = sorted({i["zone"] for i in instances if i["zone"]})
    backups = k8s.custom.list_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "backups").get("items", [])
    partitioned = _partition_active(k8s, namespace)
    snap = build_snapshot(cluster, replica_pods, zones, backups, partitioned, target)
    # topology for the SCADA view (not needed by the capability preconditions)
    snap["namespace"] = namespace
    snap["instances"] = instances
    snap["poolers"] = [
        {"name": p["metadata"]["name"], "type": p.get("spec", {}).get("type", "rw")}
        for p in k8s.custom.list_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, namespace, "poolers").get("items", [])
    ]
    return snap


def _instances(k8s: ClusterClient, namespace: str, name: str) -> list[dict]:
    """Per-instance role / zone / health for the topology view."""
    out = []
    for p in k8s.core.list_namespaced_pod(
            namespace, label_selector=f"cnpg.io/cluster={name}").items:
        labels = p.metadata.labels or {}
        if "cnpg.io/instanceRole" not in labels:
            continue   # skip pooler pods — they share cnpg.io/cluster but aren't instances
        ready = any(c.type == "Ready" and c.status == "True"
                    for c in (p.status.conditions or []))
        node = p.spec.node_name
        out.append({
            "name": p.metadata.name,
            "role": labels.get("cnpg.io/instanceRole", "?"),
            "zone": _node_zone(k8s, node) if node else "",
            "healthy": ready,
        })
    return sorted(out, key=lambda i: i["name"])


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
