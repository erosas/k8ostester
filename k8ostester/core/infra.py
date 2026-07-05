"""Shared cluster prerequisites (operators, object store).

Installed idempotently before a run, never torn down per run (D8). Each infra
entry in experiment.yaml maps to a handler here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kubernetes import client

from k8ostester.core.events import EventLog
from k8ostester.core.helm import Helm
from k8ostester.core.k8s import ClusterClient

# Pinned versions (upgrade deliberately, in one place).
CNPG_CHART_VERSION = "0.28.3"  # CloudNativePG operator 1.29.1
CNPG_REPO = "https://cloudnative-pg.github.io/charts"

INFRA_NAMESPACE = "k8ost-infra"
SEAWEEDFS_MANIFESTS = Path("infra/seaweedfs/manifests")
BACKUP_BUCKET = "backups"


class InfraManager:
    def __init__(self, k8s: ClusterClient, events: EventLog):
        self.k8s = k8s
        self.events = events

    def ensure(self, infra: list[str | dict[str, Any]]) -> None:
        for entry in infra:
            if entry == "seaweedfs":
                self._ensure_seaweedfs()
            elif isinstance(entry, dict) and entry.get("operator") == "cnpg":
                self._ensure_cnpg()
            else:
                raise ValueError(f"unknown infra entry: {entry!r}")

    def _ensure_cnpg(self) -> None:
        helm = Helm(self.k8s.context)
        self.events.emit(
            "infra.cnpg", f"ensuring CloudNativePG operator (chart {CNPG_CHART_VERSION})"
        )
        helm.repo_add("cnpg", CNPG_REPO)
        helm.upgrade_install(
            "cnpg", "cnpg/cloudnative-pg", "cnpg-system", version=CNPG_CHART_VERSION
        )

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
