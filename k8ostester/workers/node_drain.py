"""Node maintenance fault: cordon a node and evict the experiment's pods on it.

Scoped to the run namespace on purpose — the node is shared with other
namespaces (infra, other experiments) that a real drain would take down too.
Returns an uncordon cleanup: a namespace delete won't undo node mutations.

The harder variant (kill kubelet via `kubectl debug` nsenter, D1) comes later.
"""

from __future__ import annotations

from typing import Any, Callable

from k8ostester.workers.base import Worker


class NodeDrainWorker(Worker):
    name = "node_drain"

    def execute(self, target: dict[str, Any]) -> Callable[[], None] | None:
        node = self.resolve_node(target)
        self.k8s.core.patch_node(node, {"spec": {"unschedulable": True}})
        victims = [
            p.metadata.name
            for p in self.k8s.core.list_namespaced_pod(self.namespace).items
            if p.spec.node_name == node
        ]
        for pod in victims:
            self.k8s.core.delete_namespaced_pod(pod, self.namespace, grace_period_seconds=0)
        self.events.emit(
            "fault.node_drain",
            f"cordoned {node}, evicted {len(victims)} pod(s): {', '.join(victims)}",
            node=node,
            pods=victims,
        )

        def uncordon() -> None:
            self.k8s.core.patch_node(node, {"spec": {"unschedulable": False}})
            self.events.emit("fault.cleanup", f"uncordoned {node}")

        return uncordon
