"""Multi-step CNPG operations for the console — the ops the testbed golden path
performs, using the kernel client. Each fires the mutation and returns quickly;
progress shows through the cluster/pod status the console already streams. See
docs/remote-control.md.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from k8ostester_kernel.k8s import ClusterClient

from k8ostester_pg.discover import (
    ACTIVE_ROLE_ANN,
    CNPG_GROUP,
    CNPG_VERSION,
    ROTATED_AT_ANN,
)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def _cluster(k8s: ClusterClient, ns: str, name: str) -> dict:
    return k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name)


def minor_upgrade(k8s: ClusterClient, ns: str, target: str, name: str = "pg") -> str:
    """Change the cluster image — the operator rolls the replicas then switches the
    primary over. ``target`` is a full image ref (has a '/' or ':', e.g. a different
    repo) used as-is, or a bare tag applied to the current repo. Progress = phase."""
    current = _cluster(k8s, ns, name)["spec"]["imageName"]
    image = target if ("/" in target or ":" in target) else f"{current.rsplit(':', 1)[0]}:{target}"
    k8s.custom.patch_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name,
        {"spec": {"imageName": image}})
    return f"upgrade to {image} started (rolling)"


# whitelisted maintenance commands — no free-form SQL from the console
_MAINTENANCE = {
    "vacuum": "VACUUM (ANALYZE)",   # reclaim dead tuples + refresh planner stats
    "analyze": "ANALYZE",           # refresh planner stats only
    "checkpoint": "CHECKPOINT",     # flush dirty buffers to disk now
}


def maintenance(k8s: ClusterClient, ns: str, op: str, name: str = "pg") -> str:
    """Run a whitelisted maintenance command on the primary. VACUUM/ANALYZE target
    the application database; CHECKPOINT is cluster-wide. Regular VACUUM does not
    take an exclusive lock (unlike VACUUM FULL, which we deliberately don't offer)."""
    sql = _MAINTENANCE.get(op)
    if not sql:
        raise RuntimeError(f"unknown maintenance op: {op}")
    cl = _cluster(k8s, ns, name)
    primary = cl.get("status", {}).get("currentPrimary", "")
    if not primary:
        raise RuntimeError("no primary")
    db = cl.get("spec", {}).get("bootstrap", {}).get("initdb", {}).get("database", "app")
    argv = ["psql", "-U", "postgres", "-d", db, "-c", sql]
    k8s.exec_pod(ns, primary, argv, container="postgres", timeout=600)   # VACUUM can run a while
    return f"{sql} completed on {primary} ({db})"


_QTY_UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15,
              "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50}


def _qty_bytes(s: str) -> float | None:
    """Parse a k8s quantity (e.g. '10Gi', '500M', '1024') to bytes; None if unparseable."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTP]i?)?", (s or "").strip())
    if not m:
        return None
    return float(m.group(1)) * _QTY_UNITS[m.group(2) or ""]


def expand_storage(k8s: ClusterClient, ns: str, size: str, name: str = "pg") -> str:
    """Grow the data volume by patching ``spec.storage.size``. The operator expands
    each PVC in place — online, no downtime — IF the storage class allows it
    (``allowVolumeExpansion: true``). Grow-only: PostgreSQL/PVCs can't shrink."""
    size = (size or "").strip()
    if not size:
        raise RuntimeError("no target size specified")
    current = _cluster(k8s, ns, name).get("spec", {}).get("storage", {}).get("size", "")
    if size == current:
        raise RuntimeError(f"storage is already {size}")
    nb, cb = _qty_bytes(size), _qty_bytes(current)
    if nb is not None and cb is not None and nb <= cb:
        raise RuntimeError(f"storage is grow-only: {size} is not larger than {current}")
    k8s.custom.patch_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name,
        {"spec": {"storage": {"size": size}}})
    return f"storage expansion to {size} requested (was {current or '?'})"


def storage_expandable(k8s: ClusterClient, ns: str, name: str = "pg") -> tuple[bool | None, str]:
    """Can this cluster's volumes be expanded online? Returns (expandable, class).
    ``expandable`` is None when it can't be determined (missing RBAC / no class) —
    the console warns rather than blocks in that case. A False means the storage
    class lacks ``allowVolumeExpansion``, so growing the size would silently no-op."""
    cl = _cluster(k8s, ns, name)
    sc = cl.get("spec", {}).get("storage", {}).get("storageClass", "")
    if not sc:   # fall back to a data PVC's class...
        primary = cl.get("status", {}).get("currentPrimary", "")
        try:
            pvc = k8s.core.read_namespaced_persistent_volume_claim(primary, ns)
            sc = pvc.spec.storage_class_name or ""
        except Exception:
            pass
    if not sc:   # ...then the cluster's default storage class
        try:
            for c in k8s.storage.list_storage_class().items:
                if (c.metadata.annotations or {}).get(
                        "storageclass.kubernetes.io/is-default-class") == "true":
                    sc = c.metadata.name
                    break
        except Exception:
            pass
    if not sc:
        return None, ""
    try:
        return bool(k8s.storage.read_storage_class(sc).allow_volume_expansion), sc
    except Exception:
        return None, sc


def rotate_credentials(k8s: ClusterClient, ns: str, name: str = "pg",
                       password: str = "") -> str:
    """Blue/green: refresh the IDLE login role's password and switch the active
    marker to it. Both roles stay valid, so there's no auth gap. Generic — it uses
    the cluster's own managed roles and an annotation on the Cluster to track which
    role is active; it does not assume any app-side ConfigMap/Deployment. The app
    reads the active role's secret however it's wired.

    ``password`` (caller-supplied, arbitrary) is applied verbatim; if empty a
    timestamped default is generated.
    """
    cluster = _cluster(k8s, ns, name)
    roles = [r for r in cluster["spec"].get("managed", {}).get("roles", [])
             if r.get("login")]
    if len(roles) < 2:
        raise RuntimeError("blue/green rotation needs two login roles")
    anns = (cluster.get("metadata", {}) or {}).get("annotations", {}) or {}
    active = anns.get(ACTIVE_ROLE_ANN) or roles[0]["name"]
    idle = next((r for r in roles if r["name"] != active), roles[1])
    idle_secret = idle.get("passwordSecret", {}).get("name", "")
    new_pw = password or f"{idle['name']}-{_stamp()}"
    primary = cluster["status"]["currentPrimary"]
    # Dollar-quote the password literal: it needs no escaping for ANY characters
    # (quotes, backslashes, symbols) and is immune to standard_conforming_strings.
    # Bump the tag on the off-chance the password contains the delimiter. exec_pod
    # passes an argv list (no shell), so the raw bytes reach psql — no injection.
    tag = "pw"
    while f"${tag}$" in new_pw:
        tag += "x"
    k8s.exec_pod(ns, primary,
                 ["psql", "-U", "postgres", "-c",
                  f"alter role {idle['name']} password ${tag}${new_pw}${tag}$"],
                 container="postgres")
    if idle_secret:
        k8s.core.patch_namespaced_secret(idle_secret, ns,
                                         {"stringData": {"password": new_pw}})
    # record the new active role on the cluster (drives the credential view)
    k8s.custom.patch_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name,
        {"metadata": {"annotations": {ACTIVE_ROLE_ANN: idle["name"],
                                      ROTATED_AT_ANN: _stamp()}}})
    return f"rotated {active} → {idle['name']} (blue/green, no auth gap)"


def restore(k8s: ClusterClient, ns: str, target_time: str = "", name: str = "pg") -> str:
    """Bootstrap a second cluster recovering from the object store. With
    ``target_time`` (RFC3339, within the WAL window) it's point-in-time; without
    it, recover to the latest point. Uniquely-named so repeated restores don't
    clash."""
    src = _cluster(k8s, ns, name)
    store = {**src["spec"]["backup"]["barmanObjectStore"], "serverName": name}
    recovery: dict = {"source": "origin"}
    if target_time:
        recovery["recoveryTarget"] = {"targetTime": target_time}
    restore_name = f"{name}-restore-{_stamp()}"
    k8s.custom.create_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "Cluster",
            "metadata": {"name": restore_name},
            "spec": {
                "instances": 1,
                "imageName": src["spec"]["imageName"],
                "storage": src["spec"]["storage"],
                "bootstrap": {"recovery": recovery},
                "externalClusters": [{"name": "origin", "barmanObjectStore": store}],
            },
        })
    when = f"to {target_time}" if target_time else "to latest"
    return f"restore cluster {restore_name} bootstrapping (recover {when})"
