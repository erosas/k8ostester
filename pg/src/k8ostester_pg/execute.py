"""Execute a control-plane action against the cluster.

The server calls ``execute(action_id, ...)``; it re-checks the capability gate
against the current snapshot (defense in depth — a stale browser can't fire a
disabled action) and dispatches to a handler. Chaos actions map straight to
kernel primitives. Ops actions map to CNPG operations — ``backup`` is wired here;
the multi-step ops (rotate/upgrade/restore) are extracted from pg/testbed/flow.py
next. See docs/remote-control.md.
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
            params: dict | None = None) -> str:
    """Gate on the capability map, then run the action. Returns a one-line summary."""
    if not is_enabled(CNPG_ACTIONS, action_id, snapshot):
        raise ActionDenied(f"{action_id} is not enabled for the current cluster state")
    handler = _HANDLERS.get(action_id)
    if handler is None:
        raise NotImplementedError(f"no executor for {action_id!r} yet")
    return handler(k8s, namespace, snapshot, params or {})


def _kill_primary(k8s: ClusterClient, ns: str, s: dict, p: dict) -> str:
    chaos.kill_pod(k8s, ns, s["primary"])
    return f"killed primary {s['primary']}"


def _partition_primary(k8s: ClusterClient, ns: str, s: dict, p: dict) -> str:
    chaos.partition_pod(k8s, ns, s["primary"])
    return f"partitioned primary {s['primary']}"


def _kill_replica(k8s: ClusterClient, ns: str, s: dict, p: dict) -> str:
    replica = s["replicas"][0]
    chaos.kill_pod(k8s, ns, replica)
    return f"killed replica {replica}"


def _backup(k8s: ClusterClient, ns: str, s: dict, p: dict, name: str = "pg") -> str:
    backup = "console-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")   # unique per run
    k8s.custom.create_namespaced_custom_object(
        CNPG_GROUP, CNPG_VERSION, ns, "backups", {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "Backup",
            "metadata": {"name": backup},
            "spec": {"cluster": {"name": name}, "method": "barmanObjectStore"},
        })
    return f"requested base backup {backup}"


_HANDLERS = {
    "kill-primary": _kill_primary,
    "partition-primary": _partition_primary,
    "kill-replica": _kill_replica,
    "backup": _backup,
    "rotate": lambda k8s, ns, s, p: ops.rotate_credentials(k8s, ns),
    "upgrade": lambda k8s, ns, s, p: ops.minor_upgrade(k8s, ns, s["target"]),
    "restore": lambda k8s, ns, s, p: ops.restore(k8s, ns, p.get("target_time", "")),
}
