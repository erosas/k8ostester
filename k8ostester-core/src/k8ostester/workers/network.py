"""Network faults.

`network_partition` defaults to `engine: auto` (D16 revised): a deny-all
NetworkPolicy isolates the target pod at L4 with zero cluster dependencies
wherever the CNI enforces NetworkPolicy (Calico/Cilium). On a CNI that does
not (kindnet — kind/docker-desktop), auto falls back to Chaos Mesh only if it
is already installed (auto never installs anything); otherwise it uses the
native policy and warns it may not bite. `engine: netpol` / `chaos-mesh`
force one path.

`network_loss` and `network_delay` need tc-level packet manipulation inside the
pod's netns, which has no native Kubernetes API — they always use Chaos Mesh.
So does `network_partition` with `engine: chaos-mesh`. Chaos Mesh is therefore
opt-in: nothing installs it unless an experiment asks for a chaos-backed fault.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Callable

from kubernetes import client

from k8ostester.core.experiment import FaultSpec, parse_duration
from k8ostester.core.resources import load_resource
from k8ostester.workers.base import Worker

CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_PLURAL = "networkchaos"
CHAOS_CRD = "networkchaos.chaos-mesh.org"
RESOURCES = Path(__file__).parent.parent / "resources"
PARTITION_LABEL = "k8ostester.io/partition"
# CRDs that mean the CNI enforces NetworkPolicy — used only to warn when a
# native partition is unlikely to bite
_ENFORCING_CNI_CRDS = (
    "felixconfigurations.crd.projectcalico.org",  # Calico
    "ciliumnetworkpolicies.cilium.io",            # Cilium
)


class NetworkChaosWorker(Worker):
    """Chaos Mesh NetworkChaos CR — the engine for loss/delay and for the
    opt-in chaos partition. Subclasses pick the template and its variables."""

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
        # unique per injection: chaos CRs outlive their duration, and manual
        # session faults all fire at at=0s — a random suffix is collision-proof
        # even across separate k8ost processes (a module counter is not)
        name = f"k8ost-{self.name.replace('_', '-')}-{int(fault.at_s)}s-{uuid.uuid4().hex[:8]}"
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
            f"{self.name} on {pod} for {fault.duration} (chaos-mesh)",
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


class _ChaosMeshPartitionWorker(NetworkChaosWorker):
    name = "network_partition"
    template = "network-partition.yaml"


class NetworkPartitionWorker(Worker):
    """Full L4 partition. Engine (`params: {engine: ...}`):
      auto (default) — native NetworkPolicy where the CNI enforces it, else
        Chaos Mesh IF already installed (never installs it), else native+warn;
      netpol — always native (zero deps);
      chaos-mesh — always Chaos Mesh (needs the CRD / infra: chaos-mesh).
    So real clusters stay dependency-free and a kindnet dev cluster that
    happens to have chaos-mesh still enforces."""

    name = "network_partition"

    def execute(self, fault: FaultSpec) -> Callable[[], None] | None:
        engine = fault.params.get("engine", "auto")
        if engine == "auto":
            # native where it bites (zero deps); chaos-mesh only if it is
            # ALREADY installed (auto never triggers an install) and the CNI
            # would not enforce a policy — so real clusters stay dependency-free
            # while a kindnet dev cluster with chaos-mesh present still works
            engine = "netpol" if self._cni_enforces_policy() else (
                "chaos-mesh" if self.k8s.has_crd(CHAOS_CRD) else "netpol"
            )
        if engine == "chaos-mesh":
            return _ChaosMeshPartitionWorker(
                self.k8s, self.driver, self.namespace, self.events
            ).execute(fault)
        if engine != "netpol":
            raise ValueError(
                f"network_partition engine must be 'auto', 'netpol' or 'chaos-mesh', got {engine!r}"
            )
        if not fault.duration:
            raise ValueError("network_partition needs a 'duration' (e.g. duration: 60s)")

        pod = self.resolve_pod(fault.target)
        marker = uuid.uuid4().hex[:12]
        name = f"k8ost-partition-{int(fault.at_s)}s-{marker[:8]}"
        if not self._cni_enforces_policy():
            self.events.emit(
                "capability.warn",
                f"this cluster's CNI does not appear to enforce NetworkPolicy — the "
                f"partition of {pod} may not actually drop traffic; install chaos-mesh "
                "or use params: {engine: chaos-mesh} here",
            )
        # a marker label lets the policy select exactly this pod, regardless of
        # technology; removed when the partition heals
        self.k8s.core.patch_namespaced_pod(
            pod, self.namespace, {"metadata": {"labels": {PARTITION_LABEL: marker}}}
        )
        body = load_resource(
            RESOURCES / "network-partition-policy.yaml",
            {"NAME": name, "NAMESPACE": self.namespace, "MARKER": marker},
        )
        self.k8s.networking.create_namespaced_network_policy(self.namespace, body)
        self.events.emit(
            "fault.network_partition",
            f"network_partition on {pod} for {fault.duration} (NetworkPolicy)",
            pod=pod, duration=fault.duration, policy=name,
        )

        healed = threading.Event()

        def heal() -> None:
            if healed.is_set():
                return
            healed.set()
            try:
                self.k8s.networking.delete_namespaced_network_policy(name, self.namespace)
            except client.ApiException as e:
                if e.status != 404:
                    raise
            try:  # the pod may be gone (killed by another fault) — fine
                self.k8s.core.patch_namespaced_pod(
                    pod, self.namespace, {"metadata": {"labels": {PARTITION_LABEL: None}}}
                )
            except client.ApiException:
                pass
            self.events.emit("fault.cleanup", f"deleted networkpolicy/{name}")

        # NetworkPolicy has no auto-heal (unlike a chaos CR's duration), so a
        # timer heals mid-run; the returned cleanup is the teardown backstop
        timer = threading.Timer(parse_duration(fault.duration), heal)
        timer.daemon = True
        timer.start()

        def cleanup() -> None:
            timer.cancel()
            heal()

        return cleanup

    def _cni_enforces_policy(self) -> bool:
        return any(self.k8s.has_crd(crd) for crd in _ENFORCING_CNI_CRDS)


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
