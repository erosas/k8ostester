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
        "backups": _backup_view(backups),   # name/phase/times/WAL, newest first
        "retention": spec.get("backup", {}).get("retentionPolicy", ""),
        # the PITR window: WAL is archived from the earliest recoverable point to now
        "recoverability_point": status.get("firstRecoverabilityPoint", ""),
        "pitr_window": completed > 0,   # a completed base backup opens the window
        "blue_green": "app_a" in managed and "app_b" in managed,
        "sync_policy": _sync_policy(spec),     # quorum/priority synchronous config
        "object_store": _object_store(spec),   # where backups/WAL go (part of the system)
        "fault_in_flight": partitioned,
        "busy": bool(reason),           # exclusivity: a mutating op is in progress
        "busy_reason": reason,
    }


def _sync_policy(spec: dict) -> dict:
    """The synchronous-replication policy CNPG is enforcing: quorum (ANY n) or
    priority (FIRST n), or async if none. Read from spec.postgresql.synchronous
    (current CNPG) with a fallback to the older min/maxSyncReplicas."""
    syn = spec.get("postgresql", {}).get("synchronous")
    if syn:
        method = syn.get("method", "")          # "any" -> quorum, "first" -> priority
        number = int(syn.get("number", 0) or 0)
        mode = {"any": "quorum", "first": "priority"}.get(method, method or "sync")
        return {"mode": mode, "method": method, "number": number,
                "label": f"{mode} · {method} {number}".strip()}
    mx = int(spec.get("maxSyncReplicas", 0) or 0)
    if mx:
        mn = int(spec.get("minSyncReplicas", 0) or 0)
        return {"mode": "quorum", "method": "any", "number": mx,
                "label": f"quorum · sync {mn}–{mx}"}
    return {"mode": "async", "method": "", "number": 0, "label": "async (no sync standby)"}


def _object_store(spec: dict) -> dict:
    """The backup/WAL destination — bucket, path, endpoint — from barmanObjectStore."""
    store = spec.get("backup", {}).get("barmanObjectStore", {})
    dest = store.get("destinationPath", "")
    bucket = path = ""
    if dest.startswith("s3://"):
        bucket, _, path = dest[5:].partition("/")
    return {
        "configured": bool(store),
        "endpoint": store.get("endpointURL", ""),
        "bucket": bucket,
        "path": path,
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
            # WAL boundary of the backup — lets the UI count segments to replay
            "endWal": st.get("endWal", ""),
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
    # replication lag + sync state per replica, for the topology edges
    repl = _replication(k8s, namespace, snap["primary"])
    for i in snap["instances"]:
        if i["name"] in repl:
            i.update(repl[i["name"]])
    snap["archived_wal"] = _archiver(k8s, namespace, snap["primary"])
    snap["schedules"] = _schedules(k8s, namespace)          # auto-backup policy
    snap["data_size"] = _data_size(k8s, namespace, snap["primary"])
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


def _schedules(k8s: ClusterClient, namespace: str) -> list[dict]:
    """Auto-backup policy: the ScheduledBackup cron(s) governing this cluster."""
    try:
        items = k8s.custom.list_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, namespace, "scheduledbackups").get("items", [])
    except Exception:
        return []
    return [{
        "name": s.get("metadata", {}).get("name", ""),
        "schedule": s.get("spec", {}).get("schedule", ""),
        "suspend": bool(s.get("spec", {}).get("suspend", False)),
    } for s in items]


def _data_size(k8s: ClusterClient, namespace: str, primary: str) -> int | None:
    """Total on-disk data size (sum of all databases) — a proxy for how much a
    base backup carries. The Backup CR doesn't report its own byte size."""
    if not primary:
        return None
    try:
        out = k8s.exec_pod(namespace, primary,
                           ["psql", "-U", "postgres", "-tA", "-c",
                            "select coalesce(sum(pg_database_size(oid)),0)::bigint "
                            "from pg_database"],
                           container="postgres")
        return int(out.strip())
    except Exception:
        return None


def _parse_archiver(out: str) -> dict:
    """Parse 'archived|last_wal|failed' from pg_stat_archiver — the WAL segments
    (the 'parts') pushed to the object store, the newest one, and failures."""
    p = out.strip().split("|")
    if len(p) < 3:
        return {}
    try:
        return {"archived": int(p[0] or 0), "last": p[1], "failed": int(p[2] or 0)}
    except ValueError:
        return {}


def _archiver(k8s: ClusterClient, namespace: str, primary: str) -> dict:
    """WAL-archiving stats from the primary's pg_stat_archiver."""
    if not primary:
        return {}
    query = ("select archived_count, coalesce(last_archived_wal,''), failed_count "
             "from pg_stat_archiver")
    try:
        out = k8s.exec_pod(namespace, primary,
                           ["psql", "-U", "postgres", "-tAF", "|", "-c", query],
                           container="postgres")
    except Exception:
        return {}
    return _parse_archiver(out)


def _parse_replication(out: str) -> dict:
    """Parse 'app_name|sync_state|lag_bytes' lines from pg_stat_replication."""
    res: dict = {}
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0]:
            try:
                lag = int(parts[2] or 0)
            except ValueError:
                lag = 0
            res[parts[0]] = {"sync_state": parts[1], "lag_bytes": lag}
    return res


def _replication(k8s: ClusterClient, namespace: str, primary: str) -> dict:
    """Replica → {sync_state, lag_bytes} from the primary's pg_stat_replication."""
    if not primary:
        return {}
    query = ("select application_name, sync_state, "
             "coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn),0)::bigint "
             "from pg_stat_replication")
    try:
        out = k8s.exec_pod(namespace, primary,
                           ["psql", "-U", "postgres", "-tAF", "|", "-c", query],
                           container="postgres")
    except Exception:
        return {}
    return _parse_replication(out)


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
