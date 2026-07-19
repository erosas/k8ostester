"""Probe a cluster for the capabilities experiments care about.

    python -m k8ostester_kernel.capabilities --context <ctx>

Reports the signals that decide which experiments a cluster can actually run:
zones (AZ spread), worker count (node/AZ faults), whether the CNI enforces
NetworkPolicy (native partition), volume snapshots, and installed operators.
Lean by design — dataclasses + plain text, no framework.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from k8ostester_kernel.k8s import ClusterClient

# CRDs whose presence identifies an installed operator/stack.
OPERATOR_CRDS = {
    "cloudnative-pg": "clusters.postgresql.cnpg.io",
    "cnpg-pooler": "poolers.postgresql.cnpg.io",
    "chaos-mesh": "podchaos.chaos-mesh.org",
}
SNAPSHOT_CRD = "volumesnapshotclasses.snapshot.storage.k8s.io"
# CRDs that mean the CNI enforces NetworkPolicy (native partition actually drops
# traffic). kindnet (kind/docker-desktop) ships none of these.
POLICY_CNI_CRDS = (
    "felixconfigurations.crd.projectcalico.org",   # Calico
    "ciliumnetworkpolicies.cilium.io",             # Cilium
)


@dataclass
class Capabilities:
    context: str
    server_version: str
    zones: list[str]
    worker_count: int
    network_policy_enforced: bool
    snapshots: bool
    operators: dict[str, bool]

    @property
    def multi_node(self) -> bool:
        """Node/AZ-failure experiments need at least 2 schedulable workers."""
        return self.worker_count >= 2


def _snapshot_classes(k8s: ClusterClient) -> list[str]:
    try:
        listing = k8s.custom.list_cluster_custom_object(
            "snapshot.storage.k8s.io", "v1", "volumesnapshotclasses"
        )
        return [i["metadata"]["name"] for i in listing.get("items", [])]
    except Exception:
        return []


def probe(context: str | None = None) -> Capabilities:
    k8s = ClusterClient(context)
    nodes = k8s.core.list_node().items
    labels = [n.metadata.labels or {} for n in nodes]
    workers = [
        lb for lb in labels
        if not any(k.startswith("node-role.kubernetes.io/control-plane") for k in lb)
    ]
    zones = sorted({lb.get("topology.kubernetes.io/zone", "") for lb in labels} - {""})
    snap = k8s.has_crd(SNAPSHOT_CRD)
    return Capabilities(
        context=context or "(current)",
        server_version=k8s.version.get_code().git_version,
        zones=zones,
        worker_count=len(workers),
        network_policy_enforced=any(k8s.has_crd(c) for c in POLICY_CNI_CRDS),
        snapshots=snap and bool(_snapshot_classes(k8s)),
        operators={name: k8s.has_crd(crd) for name, crd in OPERATOR_CRDS.items()},
    )


def format_report(caps: Capabilities) -> str:
    def line(ok: bool, label: str, detail: str) -> str:
        return f"  {'✔' if ok else '✘'} {label:<26} {detail}"

    ops = ", ".join(n for n, ok in caps.operators.items() if ok) or "none"
    return "\n".join([
        f"cluster {caps.context}  server {caps.server_version}",
        line(caps.multi_node, "node/AZ faults", f"{caps.worker_count} worker(s)"),
        line(bool(caps.zones), "AZ spread", ", ".join(caps.zones) or "no zone labels"),
        line(caps.network_policy_enforced, "native partition",
             "CNI enforces NetworkPolicy" if caps.network_policy_enforced
             else "CNI does not enforce NetworkPolicy"),
        line(caps.snapshots, "volume snapshots",
             "snapshot class present" if caps.snapshots else "none — use object-store backups"),
        line(caps.operators.get("cloudnative-pg", False), "cloudnative-pg operator", ops),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="probe a cluster's experiment capabilities")
    ap.add_argument("--context", help="kube context (default: current)")
    print(format_report(probe(ap.parse_args().context)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
