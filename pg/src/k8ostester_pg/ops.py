"""Multi-step CNPG operations for the console — the ops the testbed golden path
performs, using the kernel client. Each fires the mutation and returns quickly;
progress shows through the cluster/pod status the console already streams. See
docs/remote-control.md.
"""
from __future__ import annotations

from datetime import UTC, datetime

from k8ostester_kernel.k8s import ClusterClient

from k8ostester_pg.discover import CNPG_GROUP, CNPG_VERSION


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def _cluster(k8s: ClusterClient, ns: str, name: str) -> dict:
    return k8s.custom.get_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name)


def minor_upgrade(k8s: ClusterClient, ns: str, target: str, name: str = "pg") -> str:
    """Bump the cluster image to the target version — the operator rolls the
    replicas then switches the primary over. Progress = the cluster phase."""
    repo = _cluster(k8s, ns, name)["spec"]["imageName"].rsplit(":", 1)[0]
    k8s.custom.patch_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "clusters", name,
        {"spec": {"imageName": f"{repo}:{target}"}})
    return f"upgrade to {target} started (rolling)"


def rotate_credentials(k8s: ClusterClient, ns: str, name: str = "pg") -> str:
    """Blue/green: refresh the IDLE role's password, flip the selector, roll the
    app onto it. Both roles stay valid, so no auth gap. Fast (no long waits)."""
    active = k8s.core.read_namespaced_config_map("app-active", ns).data["active"]
    idle = "b" if active == "a" else "a"
    new_pw = f"app-{idle}-{_stamp()}"
    primary = _cluster(k8s, ns, name)["status"]["currentPrimary"]
    # immediate ALTER ROLE (the operator's secret reconcile is not prompt)
    k8s.exec_pod(ns, primary,
                 ["psql", "-U", "postgres", "-c",
                  f"alter role app_{idle} password '{new_pw}'"],
                 container="postgres")
    k8s.core.patch_namespaced_secret(f"app-cred-{idle}", ns,
                                     {"stringData": {"password": new_pw}})
    k8s.core.patch_namespaced_config_map("app-active", ns, {"data": {"active": idle}})
    # trigger a rolling restart so the app picks up the new selector
    k8s.apps.patch_namespaced_deployment("app", ns, {"spec": {"template": {
        "metadata": {"annotations": {"k8ostester.io/rotatedAt": _stamp()}}}}})
    return f"rotated app_{active} → app_{idle} (blue/green, no auth gap)"


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
