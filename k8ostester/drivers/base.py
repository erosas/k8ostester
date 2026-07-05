"""Driver contract.

A driver owns everything technology-specific about an experiment: installing
prerequisites, deploying the config under test, readiness, topology (who is
primary — fault targeting needs it), load generation, integrity verification,
and backup/restore verbs. The runner only ever talks to this interface.

Phase 1 implements deploy/wait_ready; load, topology, integrity and backup land
with the CNPG driver in phase 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from k8ostester.core.events import EventLog
from k8ostester.core.experiment import ExperimentSpec
from k8ostester.core.k8s import ClusterClient


class TechnologyDriver:
    def __init__(
        self,
        k8s: ClusterClient,
        spec: ExperimentSpec,
        namespace: str,
        events: EventLog,
    ):
        self.k8s = k8s
        self.spec = spec
        self.namespace = namespace
        self.events = events

    def install_prereqs(self) -> None:
        """Install cluster-level prerequisites (operators, object store).
        Must be idempotent; shared across experiments, not torn down per run."""
        if self.spec.infra:
            raise NotImplementedError(f"{type(self).__name__} does not support infra")

    def deploy(self) -> None:
        """Apply the config under test into the run namespace."""
        out = self.k8s.apply_manifests(self.spec.manifests_dir, self.namespace)
        for line in out.splitlines():
            self.events.emit("manifest.applied", line)

    def wait_ready(self, timeout: float = 300) -> None:
        self.k8s.wait_workloads_ready(self.namespace, timeout)

    def topology(self) -> dict[str, Any]:
        """Role → pod mapping for fault targeting (e.g. {'primary': 'pg-1'})."""
        raise NotImplementedError

    def run_load(self, run_dir: Path) -> None:
        """Run the experiment's load plan to completion, writing metrics.jsonl
        and journal.jsonl into run_dir."""
        raise NotImplementedError(f"{type(self).__name__} has no load generator")

    def ensure_backup(self) -> None:
        """Take a base backup now (before load, so PITR can replay forward)."""
        raise NotImplementedError(f"{type(self).__name__} has no backup support")

    def verify(self, check: str, config: dict) -> dict:
        """Run a verify step; returns {check, passed, detail}."""
        raise NotImplementedError(f"{type(self).__name__} has no '{check}' verification")
