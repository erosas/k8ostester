"""K8osWorkers: fault injection. Register each worker here by its spec name."""

from __future__ import annotations

from k8ostester.workers.base import Worker
from k8ostester.workers.network import (
    NetworkDelayWorker,
    NetworkLossWorker,
    NetworkPartitionWorker,
)
from k8ostester.workers.node_drain import NodeDrainWorker
from k8ostester.workers.pod_kill import PodKillWorker

_REGISTRY: dict[str, type[Worker]] = {
    "pod_kill": PodKillWorker,
    "node_drain": NodeDrainWorker,
    "network_partition": NetworkPartitionWorker,
    "network_loss": NetworkLossWorker,
    "network_delay": NetworkDelayWorker,
}


def get_worker(name: str) -> type[Worker]:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown worker {name!r} (known: {known})")
    return _REGISTRY[name]
