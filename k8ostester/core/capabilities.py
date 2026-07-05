"""Probe a cluster and report what experiments it can support.

Experiments declare needs (multi-node faults, volume snapshots, monitoring…);
the runner uses this probe to skip or flag goals a cluster cannot exercise
instead of failing confusingly mid-run.
"""

from __future__ import annotations

import shutil
import subprocess

from pydantic import BaseModel

from .k8s import ClusterClient

# CRDs whose presence identifies an installed operator/stack.
OPERATOR_CRDS = {
    "cloudnative-pg": "clusters.postgresql.cnpg.io",
    "cnpg-pooler (pgbouncer)": "poolers.postgresql.cnpg.io",
    "prometheus-operator": "servicemonitors.monitoring.coreos.com",
    "chaos-mesh": "podchaos.chaos-mesh.org",
}

SNAPSHOT_CRD = "volumesnapshotclasses.snapshot.storage.k8s.io"


class NodeInfo(BaseModel):
    name: str
    roles: list[str]
    ready: bool
    arch: str
    kubelet_version: str


class StorageClassInfo(BaseModel):
    name: str
    provisioner: str
    is_default: bool


class Capabilities(BaseModel):
    context: str
    server_version: str
    nodes: list[NodeInfo]
    storage_classes: list[StorageClassInfo]
    snapshot_crds: bool
    snapshot_classes: list[str]
    operators: dict[str, bool]
    helm_version: str | None

    @property
    def worker_count(self) -> int:
        return sum(1 for n in self.nodes if "control-plane" not in n.roles)

    @property
    def multi_node(self) -> bool:
        """Node-failure experiments need at least 2 schedulable workers."""
        return self.worker_count >= 2

    @property
    def snapshots_supported(self) -> bool:
        return self.snapshot_crds and bool(self.snapshot_classes)


def _node_info(node) -> NodeInfo:
    roles = [
        label.removeprefix("node-role.kubernetes.io/")
        for label in node.metadata.labels
        if label.startswith("node-role.kubernetes.io/")
    ]
    ready = any(
        c.type == "Ready" and c.status == "True" for c in node.status.conditions or []
    )
    return NodeInfo(
        name=node.metadata.name,
        roles=roles or ["worker"],
        ready=ready,
        arch=node.status.node_info.architecture,
        kubelet_version=node.status.node_info.kubelet_version,
    )


def _snapshot_classes(k8s: ClusterClient) -> list[str]:
    try:
        listing = k8s.custom.list_cluster_custom_object(
            "snapshot.storage.k8s.io", "v1", "volumesnapshotclasses"
        )
        return [item["metadata"]["name"] for item in listing.get("items", [])]
    except Exception:
        return []


def _helm_version() -> str | None:
    helm = shutil.which("helm")
    if not helm:
        return None
    out = subprocess.run(
        [helm, "version", "--short"], capture_output=True, text=True, timeout=15
    )
    return out.stdout.strip() if out.returncode == 0 else None


def probe(context: str | None = None) -> Capabilities:
    k8s = ClusterClient(context)
    version = k8s.version.get_code()
    nodes = [_node_info(n) for n in k8s.core.list_node().items]
    storage_classes = [
        StorageClassInfo(
            name=sc.metadata.name,
            provisioner=sc.provisioner,
            is_default=(sc.metadata.annotations or {}).get(
                "storageclass.kubernetes.io/is-default-class"
            )
            == "true",
        )
        for sc in k8s.storage.list_storage_class().items
    ]
    snapshot_crds = k8s.has_crd(SNAPSHOT_CRD)
    return Capabilities(
        context=context or "(current)",
        server_version=version.git_version,
        nodes=nodes,
        storage_classes=storage_classes,
        snapshot_crds=snapshot_crds,
        snapshot_classes=_snapshot_classes(k8s) if snapshot_crds else [],
        operators={name: k8s.has_crd(crd) for name, crd in OPERATOR_CRDS.items()},
        helm_version=_helm_version(),
    )
