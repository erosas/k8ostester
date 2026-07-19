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


def list_clusters(k8s: ClusterClient, namespace: str | None = None) -> list[dict]:
    """Discover the CNPG clusters this context can see — the picker's inventory.

    Lists cluster-wide (all namespaces); if that's forbidden and a namespace is
    given, falls back to that namespace. Each entry is a brief health summary.
    """
    from kubernetes.client import ApiException
    try:
        items = k8s.custom.list_cluster_custom_object(
            CNPG_GROUP, CNPG_VERSION, "clusters").get("items", [])
    except ApiException as e:
        if e.status in (403, 404) and namespace:
            items = k8s.custom.list_namespaced_custom_object(
                CNPG_GROUP, CNPG_VERSION, namespace, "clusters").get("items", [])
        else:
            raise
    out = []
    for c in items:
        meta, spec, status = c["metadata"], c.get("spec", {}), c.get("status", {})
        instances = int(spec.get("instances", 0) or 0)
        ready = int(status.get("readyInstances", 0) or 0)
        out.append({
            "namespace": meta["namespace"],
            "name": meta["name"],
            "instances": instances,
            "ready": ready,
            "healthy": instances > 0 and ready == instances,
            "primary": status.get("currentPrimary", ""),
            "version": pg_version(spec.get("imageName", "")),
        })
    return sorted(out, key=lambda c: (c["namespace"], c["name"]))


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
    login = _login_roles(spec)
    completed = sum(
        1 for b in backups if b.get("status", {}).get("phase") == "completed"
    )
    phase = str(status.get("phase", ""))
    conditions = {c.get("type"): c for c in status.get("conditions", [])}
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
        "blue_green": len(login) >= 2,              # two login roles → rotation possible
        "database": _database(spec),                # app db + owner, for connection info
        "login_roles": login,                       # role -> secret, for credential view
        "credentials": _credentials(cluster, login),  # active role from the cluster itself
        "archiving": _condition(conditions, "ContinuousArchiving"),  # WAL archive health
        "sync_policy": _sync_policy(spec),     # quorum/priority synchronous config
        "object_store": _object_store(spec),   # where backups/WAL go (part of the system)
        "fault_in_flight": partitioned,
        "busy": bool(reason),           # exclusivity: a mutating op is in progress
        "busy_reason": reason,
    }


def _database(spec: dict) -> dict:
    """The application database + owner a client connects to (CNPG bootstrap)."""
    initdb = spec.get("bootstrap", {}).get("initdb", {})
    return {"name": initdb.get("database", "app"), "owner": initdb.get("owner", "app")}


def _login_roles(spec: dict) -> list[dict]:
    """Managed login roles and the secret each password comes from — the material
    a client needs, and what the credential view explores."""
    out = []
    for r in spec.get("managed", {}).get("roles", []):
        if r.get("login"):
            out.append({
                "name": r.get("name", ""),
                "secret": r.get("passwordSecret", {}).get("name", ""),
                "in_roles": r.get("inRoles", []),
            })
    return out


def _condition(conditions: dict, name: str) -> dict:
    """A CNPG status condition as {ok, message} — {} if the cluster hasn't set it."""
    c = conditions.get(name)
    if not c:
        return {}
    return {"ok": c.get("status") == "True", "message": c.get("message", "")}


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
    replica_pods = harness.replicas(k8s, namespace, name)
    instances = _instances(k8s, namespace, name)
    zones = sorted({i["zone"] for i in instances if i["zone"]})
    backups = k8s.custom.list_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "backups").get("items", [])
    partitioned = _partition_active(k8s, namespace)
    snap = build_snapshot(cluster, replica_pods, zones, backups, partitioned, target)
    # topology for the SCADA view (not needed by the capability preconditions)
    snap["namespace"] = namespace
    snap["cluster"] = name
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
    # a restore cluster still bootstrapping also locks the tool
    others = k8s.custom.list_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters").get("items", [])
    if any("-restore-" in c["metadata"]["name"]
           and int(c.get("status", {}).get("readyInstances", 0) or 0) < 1
           for c in others):
        snap["busy"] = True
        snap["busy_reason"] = snap["busy_reason"] or "restore in progress"
    return snap


_WAL_SEG_BYTES = 16 * 1024 * 1024   # default WAL segment size


def wal_seg_index(name: str) -> int | None:
    """LSN-space segment index of a 24-hex WAL filename (TLI, logical, seg).
    Independent of timeline, since the LSN of a segment doesn't depend on TLI."""
    if not name or len(name) < 24:
        return None
    try:
        return int(name[8:16], 16) * 256 + int(name[16:24], 16)
    except ValueError:
        return None


def wal_segments_since(k8s: ClusterClient, namespace: str, from_wal: str,
                       primary: str) -> dict:
    """Exact number of WAL segments from ``from_wal`` to the primary's current WAL
    insert position — i.e. how many segments a PITR bootstrapped from that base
    backup replays to reach the latest recoverable point. Computed via LSN diff."""
    frm = wal_seg_index(from_wal)
    if frm is None or not primary:
        return {}
    query = ("select floor(pg_wal_lsn_diff(pg_current_wal_lsn(),'0/0'::pg_lsn)"
             f"/{_WAL_SEG_BYTES})::bigint, pg_walfile_name(pg_current_wal_lsn())")
    try:
        out = k8s.exec_pod(namespace, primary,
                           ["psql", "-U", "postgres", "-tAF", "|", "-c", query],
                           container="postgres")
    except Exception:
        return {}
    parts = out.strip().split("|")
    try:
        current = int(parts[0])
    except (ValueError, IndexError):
        return {}
    return {"segments": max(0, current - frm),
            "current_wal": parts[1] if len(parts) > 1 else ""}


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


def heavy(k8s: ClusterClient, namespace: str, name: str = "pg") -> dict:
    """The slow-changing, expensive metrics — disk headroom, connection
    saturation, replication slots, data size. Run on a slower cadence than the
    2s topology refresh (see server.Console) so it doesn't hammer the primary."""
    cluster = k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, namespace, "clusters", name)
    primary = cluster.get("status", {}).get("currentPrimary", "")
    instances = _instances(k8s, namespace, name)
    try:
        poolers = [p["metadata"]["name"] for p in k8s.custom.list_namespaced_custom_object(
            CNPG_GROUP, CNPG_VERSION, namespace, "poolers").get("items", [])]
    except Exception:
        poolers = []
    return {
        "data_size": _data_size(k8s, namespace, primary),
        "disk": _disk(k8s, namespace, instances),
        "connections": _connections(k8s, namespace, primary),
        "slots": _slots(k8s, namespace, primary),
        "services": _services(k8s, namespace, name, poolers),
    }


def _services(k8s: ClusterClient, namespace: str, name: str,
              pooler_names: list[str]) -> list[dict]:
    """The cluster's / poolers' Services and how they're exposed — so the console
    can tell in-cluster-only from an actual external (LoadBalancer/NodePort) entry
    point, and surface the external address when one exists."""
    wanted = {f"{name}-rw", f"{name}-ro", f"{name}-r", *pooler_names}
    out = []
    try:
        svcs = k8s.core.list_namespaced_service(namespace).items
    except Exception:
        return []
    for s in svcs:
        if s.metadata.name not in wanted:
            continue
        ports = s.spec.ports or []
        ext = ""
        lb = getattr(getattr(s.status, "load_balancer", None), "ingress", None) or []
        if lb:
            ext = lb[0].hostname or lb[0].ip or ""
        out.append({
            "name": s.metadata.name,
            "type": s.spec.type or "ClusterIP",
            "port": ports[0].port if ports else 5432,
            "node_port": (ports[0].node_port if ports else None),
            "external": ext,
        })
    return out


def _parse_df(out: str) -> dict:
    """Parse `df -kP <path>` output (1K blocks) into bytes + percent used."""
    lines = [ln for ln in out.strip().splitlines() if ln]
    if len(lines) < 2:
        return {}
    f = lines[-1].split()
    if len(f) < 4:
        return {}
    try:
        size, used = int(f[1]) * 1024, int(f[2]) * 1024
    except ValueError:
        return {}
    return {"size": size, "used": used, "pct": round(used / size * 100, 1) if size else 0}


def _disk(k8s: ClusterClient, namespace: str, instances: list[dict]) -> dict:
    """Data-volume usage per instance — the disk-full early warning."""
    out = {}
    for i in instances:
        try:
            raw = k8s.exec_pod(namespace, i["name"],
                               ["df", "-kP", "/var/lib/postgresql/data"],
                               container="postgres")
        except Exception:
            continue
        d = _parse_df(raw)
        if d:
            out[i["name"]] = d
    return out


def _parse_conn(out: str) -> dict:
    p = out.strip().split("|")
    try:
        return {"active": int(p[0]), "max": int(p[1])}
    except (ValueError, IndexError):
        return {}


def _connections(k8s: ClusterClient, namespace: str, primary: str) -> dict:
    """Active backends vs max_connections — connection-saturation headroom."""
    if not primary:
        return {}
    query = ("select (select count(*) from pg_stat_activity), "
             "(select setting::int from pg_settings where name='max_connections')")
    try:
        raw = k8s.exec_pod(namespace, primary,
                           ["psql", "-U", "postgres", "-tAF", "|", "-c", query],
                           container="postgres")
    except Exception:
        return {}
    return _parse_conn(raw)


def _parse_slots(out: str) -> list[dict]:
    slots = []
    for line in out.strip().splitlines():
        p = line.split("|")
        if len(p) >= 3 and p[0]:
            try:
                retained = int(p[2] or 0)
            except ValueError:
                retained = 0
            slots.append({"name": p[0], "active": p[1] == "t", "retained_bytes": retained})
    return slots


def _slots(k8s: ClusterClient, namespace: str, primary: str) -> list[dict]:
    """Replication slots — an inactive slot silently pins WAL and fills disk."""
    if not primary:
        return []
    query = ("select slot_name, active, "
             "coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn),0)::bigint "
             "from pg_replication_slots order by 1")
    try:
        raw = k8s.exec_pod(namespace, primary,
                           ["psql", "-U", "postgres", "-tAF", "|", "-c", query],
                           container="postgres")
    except Exception:
        return []
    return _parse_slots(raw)


def _parse_archiver(out: str) -> dict:
    """Parse 'archived|last_wal|failed|last_time_epoch|current_wal' from
    pg_stat_archiver + current WAL. Adds ``lag_segments`` — how many WAL segments
    the archiver is behind the primary's current position (0 = caught up)."""
    p = out.strip().split("|")
    if len(p) < 3:
        return {}
    try:
        res = {"archived": int(p[0] or 0), "last": p[1], "failed": int(p[2] or 0)}
    except ValueError:
        return {}
    if len(p) >= 5:
        res["last_time"] = p[3]          # epoch seconds (str), or "" if never archived
        res["current"] = p[4]
        a, b = wal_seg_index(p[1]), wal_seg_index(p[4])
        if a is not None and b is not None:
            res["lag_segments"] = max(0, b - a)
    return res


def _archiver(k8s: ClusterClient, namespace: str, primary: str) -> dict:
    """WAL-archiving stats + freshness/lag from the primary's pg_stat_archiver."""
    if not primary:
        return {}
    query = ("select archived_count, coalesce(last_archived_wal,''), failed_count, "
             "coalesce(extract(epoch from last_archived_time)::bigint::text,''), "
             "(select pg_walfile_name(pg_current_wal_lsn())) from pg_stat_archiver")
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


ACTIVE_ROLE_ANN = "k8ostester.io/active-role"
ROTATED_AT_ANN = "k8ostester.io/rotatedAt"


def _credentials(cluster: dict, login_roles: list[dict]) -> dict:
    """Which login role is active, and when it last rotated — tracked on the
    Cluster itself (an annotation), not app-side wiring. Defaults to the first
    login role until a rotation records a choice."""
    anns = (cluster.get("metadata", {}) or {}).get("annotations", {}) or {}
    names = [r["name"] for r in login_roles]
    active = anns.get(ACTIVE_ROLE_ANN) or (names[0] if names else "")
    return {
        "active_role": active,
        "rotated_at": anns.get(ROTATED_AT_ANN, ""),
        "roles": names,
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
            "node": node or "",                 # physical k8s node placement
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
