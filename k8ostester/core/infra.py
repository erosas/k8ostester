"""COMMON cluster prerequisites (object store, monitoring).

Installed idempotently before a run, never torn down per run (D8). Core only
owns infra any technology can use; technology-specific prerequisites (e.g. the
CNPG operator) are installed by the technology's own driver (D15), which
delegates the common entries here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kubernetes import client

from k8ostester.core.events import EventLog
from k8ostester.core.helm import Helm
from k8ostester.core.k8s import ClusterClient

# Pinned versions of COMMON infra (tech-specific pins live in each driver).
CHAOS_MESH_CHART_VERSION = "2.8.3"  # network fault engine (Apache 2.0, D16)
CHAOS_MESH_REPO = "https://charts.chaos-mesh.org"

INFRA_NAMESPACE = "k8ost-infra"
CHAOS_NAMESPACE = "k8ost-chaos"
BACKUP_BUCKET = "backups"

# framework-owned defaults (shipped in the package); a config repo can
# override either by placing the same relative path under its own CWD
# (e.g. infra/chaos-mesh/values.yaml with a different runtime socket)
_PACKAGED = Path(__file__).parent.parent / "resources" / "infra"


def _infra_path(override: str, packaged: str) -> Path:
    local = Path(override)
    return local.resolve() if local.exists() else _PACKAGED / packaged


class InfraManager:
    def __init__(self, k8s: ClusterClient, events: EventLog):
        self.k8s = k8s
        self.events = events

    def handles(self, entry: str | dict[str, Any]) -> bool:
        return entry in ("seaweedfs", "chaos-mesh")

    def ensure(self, infra: list[str | dict[str, Any]]) -> None:
        for entry in infra:
            if entry == "seaweedfs":
                self._ensure_seaweedfs()
            elif entry == "chaos-mesh":
                self._ensure_chaos_mesh()
            else:
                raise ValueError(f"not common infra: {entry!r} (does the tech driver handle it?)")

    def _ensure_chaos_mesh(self) -> None:
        """Chaos Mesh: the engine behind the network_* fault workers (D16)."""
        values = _infra_path("infra/chaos-mesh/values.yaml", "chaos-mesh/values.yaml")
        self.events.emit("infra.chaos-mesh", f"ensuring chaos-mesh {CHAOS_MESH_CHART_VERSION}")
        helm = Helm(self.k8s.context)
        helm.repo_add("chaos-mesh", CHAOS_MESH_REPO)
        helm.upgrade_install(
            "chaos-mesh", "chaos-mesh/chaos-mesh", CHAOS_NAMESPACE,
            version=CHAOS_MESH_CHART_VERSION, values_file=values,
        )
        self.events.emit("infra.chaos-mesh", "chaos-mesh ready")

    def _ensure_seaweedfs(self) -> None:
        self.events.emit("infra.seaweedfs", "ensuring SeaweedFS object store")
        self._ensure_namespace(INFRA_NAMESPACE)
        manifests = _infra_path("infra/seaweedfs", "seaweedfs")
        self.k8s.apply_manifests(manifests, INFRA_NAMESPACE)
        self.k8s.wait_workloads_ready(INFRA_NAMESPACE, timeout=300)
        self._ensure_bucket(BACKUP_BUCKET)

    def _ensure_namespace(self, name: str) -> None:
        try:
            self.k8s.core.read_namespace(name)
        except client.ApiException as e:
            if e.status != 404:
                raise
            self.k8s.create_namespace(name)

    def _ensure_bucket(self, bucket: str) -> None:
        pods = self.k8s.core.list_namespaced_pod(
            INFRA_NAMESPACE, label_selector="app=seaweedfs"
        ).items
        pod = next(p.metadata.name for p in pods if p.status.phase == "Running")
        out = self.k8s.exec_pod(
            INFRA_NAMESPACE,
            pod,
            ["sh", "-c", f'echo "s3.bucket.create -name {bucket}" | weed shell 2>&1'],
        )
        # existing bucket is fine — creation is idempotent for our purposes
        if "error" in out.lower() and "already exists" not in out.lower():
            raise RuntimeError(f"bucket creation failed: {out.strip()}")
        self.events.emit("infra.seaweedfs", f"bucket '{bucket}' ready")
