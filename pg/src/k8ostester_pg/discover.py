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
    phase = str(status.get("phase", ""))
    reason = _busy_reason(phase, backups)   # a mutating op is in flight → lock
    return {
        "ready": instances > 0 and ready_n == instances,
        "phase": phase,                 # the cluster's own status line (live)
        "primary": status.get("currentPrimary", ""),
        "replicas": replica_pods,
        "zones": zones,
        "version": pg_version(spec.get("imageName", "")),
        # target may be a full image (…:tag) or a bare version
        "target": (pg_version(target) if ":" in target else target) if target else "",
        "upgrading": "upgrad" in phase.lower(),
        "backup_configured": "backup" in spec,
        "backups_completed": completed,
        "backups": _backup_view(backups),   # name/phase/times, newest first
        # the PITR window: WAL is archived from the earliest recoverable point to now
        "recoverability_point": status.get("firstRecoverabilityPoint", ""),
        "pitr_window": completed > 0,   # a completed base backup opens the window
        "blue_green": "app_a" in managed and "app_b" in managed,
        "fault_in_flight": partitioned,
        "busy": bool(reason),           # exclusivity: a mutating op is in progress
        "busy_reason": reason,
    }


def _backup_view(backups: list[dict]) -> list[dict]:
    """Recent backups with phase + times (for the timeline), newest first."""
    ordered = sorted(
        backups,
        key=lambda b: b.get("metadata", {}).get("creationTimestamp", ""),
        reverse=True,
    )
    out = []
    for b in ordered[:10]:
        st = b.get("status", {})
        out.append({
            "name": b.get("metadata", {}).get("name", ""),
            "phase": st.get("phase", ""),
            "startedAt": st.get("startedAt", ""),
            "stoppedAt": st.get("stoppedAt", ""),
        })
    return out


def _busy_reason(phase: str, backups: list[dict]) -> str:
    """A mutating operation the tool should not overlap. Chaos faults are not
    counted here — they stay available (with an ack)."""
    if any(b.get("status", {}).get("phase") in ("running", "started") for b in backups):
        return "base backup running"
    if "upgrad" in phase.lower():
        return "upgrading"
    return ""


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
    snap["credentials"] = _credentials(k8s, namespace, snap["blue_green"])
    # a restore cluster still bootstrapping also locks the tool
    others = k8s.custom.list_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters").get("items", [])
    if any("-restore-" in c["metadata"]["name"]
           and int(c.get("status", {}).get("readyInstances", 0) or 0) < 1
           for c in others):
        snap["busy"] = True
        snap["busy_reason"] = snap["busy_reason"] or "restore in progress"
    return snap


def _credentials(k8s: ClusterClient, namespace: str, blue_green: bool) -> dict:
    """Which blue/green role the app authenticates as, and when it was last set."""
    active = rotated = ""
    try:
        active = k8s.core.read_namespaced_config_map("app-active", namespace).data.get("active", "")
    except Exception:
        pass
    try:
        dep = k8s.apps.read_namespaced_deployment("app", namespace)
        rotated = (dep.spec.template.metadata.annotations or {}).get("k8ostester.io/rotatedAt", "")
    except Exception:
        pass
    return {
        "active": active,
        "active_role": f"app_{active}" if active else "",
        "rotated_at": rotated,
        "roles": ["app_a", "app_b"] if blue_green else [],
    }


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
