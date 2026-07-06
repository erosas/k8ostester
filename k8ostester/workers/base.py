"""Fault worker contract.

A worker performs one fault action against a resolved target. Targets are
resolved at injection time (not run start) because topology changes as faults
land — after a failover, "primary" is a different pod.

execute() may return a cleanup callable for cluster-level mutations that a
namespace delete won't undo (e.g. uncordoning a node); the runner invokes all
cleanups during teardown.
"""

from __future__ import annotations

from typing import Any, Callable

from k8ostester.core.events import EventLog
from k8ostester.core.k8s import ClusterClient
from k8ostester.drivers.base import TechnologyDriver


class Worker:
    name = "worker"

    def __init__(
        self,
        k8s: ClusterClient,
        driver: TechnologyDriver,
        namespace: str,
        events: EventLog,
    ):
        self.k8s = k8s
        self.driver = driver
        self.namespace = namespace
        self.events = events

    def execute(self, target: dict[str, Any]) -> Callable[[], None] | None:
        raise NotImplementedError

    def resolve_pod(self, target: dict[str, Any]) -> str:
        """{pod: name} directly, or {role: primary|replica} via driver topology."""
        if "pod" in target:
            return target["pod"]
        if "role" in target:
            topology = self.driver.topology()
            role = target["role"]
            if role == "primary":
                return topology["primary"]
            if role == "replica":
                replicas = topology.get("replicas", [])
                if not replicas:
                    raise RuntimeError("no replica to target")
                return replicas[0]
            raise ValueError(f"unknown role {role!r} in target")
        raise ValueError(f"target needs 'pod', 'role' or 'node_of': {target!r}")

    def resolve_node(self, target: dict[str, Any]) -> str:
        """{node: name} directly, or {node_of: primary|replica} via the pod's node."""
        if "node" in target:
            return target["node"]
        if "node_of" in target:
            pod = self.resolve_pod({"role": target["node_of"]})
            return self.k8s.core.read_namespaced_pod(pod, self.namespace).spec.node_name
        raise ValueError(f"target needs 'node' or 'node_of': {target!r}")
