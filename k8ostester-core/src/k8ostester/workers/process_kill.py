"""Kill the main process inside the target pod's container (kill -9 PID 1).

The scoped alternative to taking a node down: the workload experiences sudden
process death (crash, OOM) and kubelet restarts the container *in place* —
same pod, same IP, nothing else on the node or cluster feels it. Contrast
with pod_kill (an API-visible delete: endpoints update immediately) and with
network_partition (silence, no RST — the closest per-pod stand-in for a dark
node). A true whole-node fault stays node_drain / a future kubelet kill,
which affect every tenant on the node — the concurrent-run guard exists for
exactly that blast radius.
"""

from __future__ import annotations

from collections.abc import Callable

from k8ostester.core.experiment import FaultSpec
from k8ostester.workers.base import Worker


class ProcessKillWorker(Worker):
    name = "process_kill"

    def execute(self, fault: FaultSpec) -> Callable[[], None] | None:
        pod = self.resolve_pod(fault.target)
        container = fault.params.get("container")
        try:
            self.k8s.exec_pod(
                self.namespace, pod, ["sh", "-c", "kill -9 1"], container=container
            )
        except RuntimeError:
            # PID 1 dying tears down the exec stream itself — a non-zero exit
            # here usually means the kill landed, not that it failed
            pass
        self.events.emit(
            "fault.process_kill",
            f"kill -9 pid 1 in {pod}" + (f" ({container})" if container else ""),
            pod=pod,
            container=container,
        )
        return None