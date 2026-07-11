"""Kill a pod without grace — the sudden-death fault (OOM kill, crash, eviction)."""

from __future__ import annotations

from typing import Callable

from k8ostester.core.experiment import FaultSpec
from k8ostester.workers.base import Worker


class PodKillWorker(Worker):
    name = "pod_kill"

    def execute(self, fault: FaultSpec) -> Callable[[], None] | None:
        pod = self.resolve_pod(fault.target)
        self.k8s.core.delete_namespaced_pod(pod, self.namespace, grace_period_seconds=0)
        self.events.emit("fault.pod_kill", f"killed {pod} (grace 0)", pod=pod)
        return None
