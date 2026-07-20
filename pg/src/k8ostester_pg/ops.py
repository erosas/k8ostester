"""Multi-step CNPG operations for the console — the ops the testbed golden path
performs, using the kernel client. Each fires the mutation and returns quickly;
progress shows through the cluster/pod status the console already streams. See
docs/remote-control.md.
"""
from __future__ import annotations

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
    k8s.custom.patch_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name,
        {"spec": {"storage": {"size": size}}})
    return f"storage expansion to {size} requested (was {current or '?'})"


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
