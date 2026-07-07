"""Network faults via Chaos Mesh (D16): partition, packet loss, added latency.

These are the faults we wrap rather than build — tc/iptables-level injection
inside the pod's netns is genuinely hard. Each worker renders a NetworkChaos
CR template into the run namespace; Chaos Mesh auto-heals the fault after
`duration`, and the returned cleanup deletes the CR as a belt-and-braces
teardown (an early-aborted run must not leave a partition behind).

Requires the `chaos-mesh` common infra entry in the experiment's infra list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from kubernetes import client

from k8ostester.core.experiment import FaultSpec
from k8ostester.core.resources import load_resource
from k8ostester.workers.base import Worker

CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURAL = "networkchaos"
CHAOS_CRD = "networkchaos.chaos-mesh.org"
RESOURCES = Path(__file__).parent.parent / "resources"


class NetworkChaosWorker(Worker):
    """Shared plumbing; subclasses pick the template and its extra variables."""

    template = "override-me.yaml"

    def extra_variables(self, fault: FaultSpec) -> dict[str, str]:
        return {}

    def execute(self, fault: FaultSpec) -> Callable[[], None] | None:
        if not self.k8s.has_crd(CHAOS_CRD):
            raise RuntimeError(
                f"{self.name} needs Chaos Mesh — add 'chaos-mesh' to the experiment's infra list"
            )
        if not fault.duration:
            raise ValueError(f"{self.name} needs a 'duration' (e.g. duration: 60s)")
        pod = self.resolve_pod(fault.target)
        name = f"k8ost-{self.name.replace('_', '-')}-{int(fault.at_s)}s"
        body = load_resource(
            RESOURCES / self.template,
            {
                "NAME": name,
                "NAMESPACE": self.namespace,
                "PODS": json.dumps({self.namespace: [pod]}),
                "DURATION": fault.duration,
                **self.extra_variables(fault),
            },
        )
        self.k8s.custom.create_namespaced_custom_object(
            CHAOS_GROUP, CHAOS_VERSION, self.namespace, CHAOS_PLURAL, body
        )
        self.events.emit(
            f"fault.{self.name}",
            f"{self.name} on {pod} for {fault.duration}",
            pod=pod,
            duration=fault.duration,
            chaos=name,
        )

        def delete_chaos() -> None:
            try:
                self.k8s.custom.delete_namespaced_custom_object(
                    CHAOS_GROUP, CHAOS_VERSION, self.namespace, CHAOS_PLURAL, name
                )
                self.events.emit("fault.cleanup", f"deleted networkchaos/{name}")
            except client.ApiException as e:
                if e.status != 404:  # namespace teardown may have raced us
                    raise

        return delete_chaos


class NetworkPartitionWorker(NetworkChaosWorker):
    name = "network_partition"
    template = "network-partition.yaml"


class NetworkLossWorker(NetworkChaosWorker):
    name = "network_loss"
    template = "network-loss.yaml"

    def extra_variables(self, fault: FaultSpec) -> dict[str, str]:
        return {"LOSS": str(fault.params.get("loss", "50"))}


class NetworkDelayWorker(NetworkChaosWorker):
    name = "network_delay"
    template = "network-delay.yaml"

    def extra_variables(self, fault: FaultSpec) -> dict[str, str]:
        return {
            "LATENCY": str(fault.params.get("latency", "100ms")),
            "JITTER": str(fault.params.get("jitter", "0ms")),
        }