"""Execute a control-plane action against the cluster.

The server calls ``execute(action_id, ...)``; it re-checks the capability gate
against the current snapshot (defense in depth — a stale browser can't fire a
disabled action) and dispatches to a handler. Chaos actions map straight to
kernel primitives. Ops actions map to CNPG operations — ``backup`` is wired here;
the multi-step ops (rotate/upgrade/restore) live in ``ops.py``. See
docs/remote-control.md.
"""
from __future__ import annotations

from datetime import UTC, datetime

from k8ostester_kernel import chaos
from k8ostester_kernel.control import is_enabled
from k8ostester_kernel.k8s import ClusterClient

from k8ostester_pg import ops
from k8ostester_pg.control import CNPG_ACTIONS
from k8ostester_pg.discover import CNPG_GROUP, CNPG_VERSION


class ActionDenied(Exception):
    """The action's precondition does not hold against the current state."""


def execute(k8s: ClusterClient, namespace: str, action_id: str, snapshot: dict,
            params: dict | None = None, name: str = "pg") -> str:
    """Gate on the capability map, then run the action. Returns a one-line summary."""
    if not is_enabled(CNPG_ACTIONS, action_id, snapshot):
        raise ActionDenied(f"{action_id} is not enabled for the current cluster state")
    handler = _HANDLERS.get(action_id)
    if handler is None:
        raise NotImplementedError(f"no executor for {action_id!r} yet")
    return handler(k8s, namespace, snapshot, params or {}, name)


def _target_pod(s: dict, p: dict) -> str:
    """The fault target: the requested pod if it's a real instance, else default
    to the primary (so a fault always hits something valid)."""
    valid = {s.get("primary"), *(s.get("replicas") or [])}
    valid.discard("")
    valid.discard(None)
    pod = p.get("pod")
    if pod not in valid:
        pod = s.get("primary") or next(iter(s.get("replicas") or []), "")
    if not pod:
        raise ActionDenied("no target pod")
    return pod


def _kill_pod(k8s: ClusterClient, ns: str, s: dict, p: dict, name: str) -> str:
    pod = _target_pod(s, p)
    chaos.kill_pod(k8s, ns, pod)
    return f"killed pod {pod}"


def _partition_pod(k8s: ClusterClient, ns: str, s: dict, p: dict, name: str) -> str:
    pod = _target_pod(s, p)
    chaos.partition_pod(k8s, ns, pod)
    return f"partitioned pod {pod}"


def _backup(k8s: ClusterClient, ns: str, s: dict, p: dict, name: str) -> str:
    backup = "console-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")   # unique per run
    k8s.custom.create_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "backups", {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "Backup",
            "metadata": {"name": backup},
            "spec": {"cluster": {"name": name}, "method": "barmanObjectStore"},
        })
    return f"requested base backup {backup}"


def _upgrade(k8s: ClusterClient, ns: str, s: dict, p: dict, name: str) -> str:
    target = p.get("target") or s.get("target", "")   # chosen in the modal at press time
    if not target:
        raise ActionDenied("no target image/version specified")
    return ops.minor_upgrade(k8s, ns, target, name)


_HANDLERS = {
    "kill-pod": _kill_pod,
    "partition-pod": _partition_pod,
    "backup": _backup,
    "rotate": lambda k8s, ns, s, p, name: ops.rotate_credentials(
        k8s, ns, name, p.get("password", "")),
    "upgrade": _upgrade,
    "expand-storage": lambda k8s, ns, s, p, name: ops.expand_storage(
        k8s, ns, p.get("size", ""), name),
    "maintenance": lambda k8s, ns, s, p, name: ops.maintenance(
        k8s, ns, p.get("op", ""), name),
    "restore": lambda k8s, ns, s, p, name: ops.restore(k8s, ns, p.get("target_time", ""), name),
}
