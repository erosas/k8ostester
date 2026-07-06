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
KPS_CHART_VERSION = "87.10.1"  # kube-prometheus-stack (prometheus-operator 0.92)
KPS_REPO = "https://prometheus-community.github.io/helm-charts"
PERSES_CHART_VERSION = "0.22.0"  # Perses 0.53 (Apache 2.0 dashboards, D7)
PERSES_REPO = "https://perses.github.io/helm-charts"

INFRA_NAMESPACE = "k8ost-infra"
MONITORING_NAMESPACE = "k8ost-monitoring"
SEAWEEDFS_MANIFESTS = Path("infra/seaweedfs/manifests")
MONITORING_DIR = Path("infra/monitoring")
BACKUP_BUCKET = "backups"


class InfraManager:
    def __init__(self, k8s: ClusterClient, events: EventLog):
        self.k8s = k8s
        self.events = events

    def handles(self, entry: str | dict[str, Any]) -> bool:
        return entry in ("seaweedfs", "monitoring")

    def ensure(self, infra: list[str | dict[str, Any]]) -> None:
        for entry in infra:
            if entry == "seaweedfs":
                self._ensure_seaweedfs()
            elif entry == "monitoring":
                self._ensure_monitoring()
            else:
                raise ValueError(f"not common infra: {entry!r} (does the tech driver handle it?)")

    def _ensure_monitoring(self) -> None:
        """Prometheus stack (Grafana disabled — AGPL, D7) + Perses dashboards."""
        helm = Helm(self.k8s.context)
        monitoring = MONITORING_DIR.resolve()
        if not monitoring.is_dir():
            raise FileNotFoundError(f"{monitoring} not found — run from the repository root")
        self.events.emit(
            "infra.monitoring",
            f"ensuring kube-prometheus-stack {KPS_CHART_VERSION} + perses {PERSES_CHART_VERSION}",
        )
        self._ensure_namespace(MONITORING_NAMESPACE)
        self.k8s.apply_manifests(monitoring / "manifests", MONITORING_NAMESPACE)
        helm.repo_add("prometheus-community", KPS_REPO)
        helm.upgrade_install(
            "kps", "prometheus-community/kube-prometheus-stack", MONITORING_NAMESPACE,
            version=KPS_CHART_VERSION, values_file=monitoring / "kps-values.yaml",
        )
        helm.repo_add("perses", PERSES_REPO)
        helm.upgrade_install(
            "perses", "perses/perses", MONITORING_NAMESPACE,
            version=PERSES_CHART_VERSION, values_file=monitoring / "perses-values.yaml",
        )
        self.events.emit("infra.monitoring", "monitoring stack ready")

    def _ensure_seaweedfs(self) -> None:
        self.events.emit("infra.seaweedfs", "ensuring SeaweedFS object store")
        self._ensure_namespace(INFRA_NAMESPACE)
        manifests = SEAWEEDFS_MANIFESTS.resolve()
        if not manifests.is_dir():
            raise FileNotFoundError(
                f"{manifests} not found — run from the repository root"
            )
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
